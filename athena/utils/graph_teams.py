"""Microsoft Graph Teams online-meeting client & diagnostic probe.

Pure Graph glue over ``utils.graph`` — zero ``models`` and zero ``flask``
imports (main service only; calling from the portal raises GraphNotConfigured).
Error messages carry HTTP status codes or exception types only, never client
names, email addresses, subjects, or tokens.
"""

from __future__ import annotations

import base64
import html
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from config import Config
from tz import MTL
from utils import graph
from utils.graph import GraphError, GraphNotConfigured
from utils.graph_calendrier import MIROIR_CATEGORIE

REQUIRED_GRAPH_ROLES: tuple[str, ...] = (
    "Calendars.ReadWrite",
    "Mail.Send",
    "OnlineMeetings.ReadWrite.All",
    "OnlineMeetingTranscript.Read.All",
    "OnlineMeetingAiInsight.Read.All",
)

def _probe_join_url(tenant_id: str, user_id: str) -> str:
    """Build a syntactically valid Teams meetup-join URL for policy probing."""
    ctx = quote(
        json.dumps({"Tid": tenant_id, "Oid": user_id}, separators=(",", ":")),
        safe="",
    )
    return (
        "https://teams.microsoft.com/l/meetup-join/"
        "19%3ameeting_00000000-0000-0000-0000-000000000000%40thread.v2/"
        f"0?context={ctx}"
    )


def powershell_setup_commands(
    client_id: str = "", juriste_upn: str = ""
) -> list[str]:
    """Return the one-time Teams Admin PowerShell commands for this deployment."""
    cid = (client_id or "").strip() or "<GRAPH_CLIENT_ID>"
    upn = (juriste_upn or "").strip() or "<BOOKINGS_JURISTE_UPN>"
    return [
        "Connect-MicrosoftTeams",
        "Set-CsTeamsMeetingConfiguration -EnableGraphTranscriptAccess $true",
        (
            'New-CsApplicationAccessPolicy -Identity "PallasAthenaTeamsAccess" '
            f'-AppIds "{cid}" '
            '-Description "Pallas Athena Teams meetings, transcripts and Copilot insights"'
        ),
        (
            'Grant-CsApplicationAccessPolicy -PolicyName "PallasAthenaTeamsAccess" '
            f'-Identity "{upn}"'
        ),
        (
            "Set-CsTeamsMeetingPolicy -Identity Global "
            "-Copilot EnabledWithTranscriptDefaultOn "
            "-AutomaticallyStartCopilot Enabled"
        ),
    ]


def _escape_multiline(text: str) -> str:
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return ""
    return "<br>".join(html.escape(line, quote=True) for line in cleaned.split("\n"))


def construire_corps_html_reunion(
    ordre_du_jour: str, points_discussion: str = ""
) -> str:
    """Build an escaped HTML invite body carrying the timed agenda & notice."""
    parts: list[str] = ["<div>"]
    agenda_html = _escape_multiline(ordre_du_jour)
    if agenda_html:
        parts.append("<p><strong>Ordre du jour minuté</strong></p>")
        parts.append(f"<p>{agenda_html}</p>")
    points_html = _escape_multiline(points_discussion)
    if points_html:
        parts.append("<p><strong>Points à aborder</strong></p>")
        parts.append(f"<p>{points_html}</p>")
    parts.append(
        "<hr><p><em>Avis : la transcription et la prise de notes assistée par "
        "intelligence artificielle (Microsoft Teams) peuvent être utilisées lors "
        "de cette rencontre, sous réserve de votre confirmation en début "
        "d'appel.</em></p>"
    )
    parts.append("</div>")
    return "".join(parts)


def _utc_graph_dt(dt: datetime) -> dict[str, str]:
    utc_dt = (
        dt.replace(tzinfo=timezone.utc)
        if dt.tzinfo is None
        else dt.astimezone(timezone.utc)
    )
    return {
        "dateTime": utc_dt.strftime("%Y-%m-%dT%H:%M:%S"),
        "timeZone": "UTC",
    }


def _is_not_found(exc: GraphError) -> bool:
    return "HTTP 404" in str(exc)


def _is_copilot_unavailable(exc: GraphError) -> bool:
    msg = str(exc)
    return "HTTP 400" in msg or "HTTP 403" in msg


MAX_TRANSCRIPT_VTT_BYTES = 5 * 1024 * 1024
_VTT_CHUNK_BYTES = 65_536


def _require_teams_user_id() -> str:
    if not Config.teams_configured():
        raise GraphNotConfigured(
            "L'intégration Microsoft Teams n'est pas configurée."
        )
    return quote(Config.BOOKINGS_JURISTE_OBJECT_ID.strip(), safe="")


_FRAC_RE = re.compile(r"\.(\d{1,})")
_WINDOWS_EASTERN_TZ = frozenset({
    "eastern standard time",
    "us eastern standard time",
    "america/toronto",
    "america/montreal",
})


def _parse_schedule_dt(obj: Any) -> datetime:
    """Parse a Graph schedule ``{dateTime, timeZone}`` object into UTC (fail-closed)."""
    if not isinstance(obj, dict):
        raise GraphError("Horodatage Graph getSchedule invalide.")
    raw = str(obj.get("dateTime") or "").strip()
    if not raw:
        raise GraphError("Horodatage Graph getSchedule manquant.")
    has_z = raw.endswith(("Z", "z"))
    cleaned = raw.rstrip("Zz")
    cleaned = _FRAC_RE.sub(lambda m: "." + m.group(1)[:6], cleaned)
    try:
        dt = datetime.fromisoformat(cleaned)
    except ValueError as exc:
        raise GraphError("Format de date Graph getSchedule invalide.") from exc
    if dt.tzinfo is None:
        if has_z:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            tz_name = str(obj.get("timeZone") or "").strip()
            low = tz_name.lower()
            if not tz_name or low in ("utc", "coordinated universal time"):
                dt = dt.replace(tzinfo=timezone.utc)
            elif low in _WINDOWS_EASTERN_TZ:
                dt = dt.replace(tzinfo=MTL)
            else:
                try:
                    dt = dt.replace(tzinfo=ZoneInfo(tz_name))
                except Exception as exc:
                    raise GraphError("Fuseau horaire Graph getSchedule inconnu.") from exc
    return dt.astimezone(timezone.utc)


def lire_plages_occupees(
    debut_utc: datetime, fin_utc: datetime
) -> list[tuple[datetime, datetime]]:
    """Read busy Outlook intervals in UTC via Graph ``getSchedule`` (chunked <= 60 days)."""
    if not Config.teams_configured():
        raise GraphNotConfigured(
            "L'intégration Microsoft Teams / calendrier Graph n'est pas configurée."
        )
    utc_start = (
        debut_utc.replace(tzinfo=timezone.utc)
        if debut_utc.tzinfo is None
        else debut_utc.astimezone(timezone.utc)
    )
    utc_end = (
        fin_utc.replace(tzinfo=timezone.utc)
        if fin_utc.tzinfo is None
        else fin_utc.astimezone(timezone.utc)
    )
    if utc_end <= utc_start:
        return []

    upn = Config.BOOKINGS_JURISTE_UPN.strip()
    encoded_upn = quote(upn, safe="")
    upn_lower = upn.lower()
    chunk = timedelta(days=60)
    intervals: list[tuple[datetime, datetime]] = []

    cursor = utc_start
    while cursor < utc_end:
        chunk_end = min(cursor + chunk, utc_end)
        resp = graph.graph_post(
            f"/users/{encoded_upn}/calendar/getSchedule",
            {
                "schedules": [upn],
                "startTime": _utc_graph_dt(cursor),
                "endTime": _utc_graph_dt(chunk_end),
                "availabilityViewInterval": 15,
            },
        )
        values = (resp or {}).get("value") if isinstance(resp, dict) else None
        if not isinstance(values, list) or not values:
            raise GraphError("Réponse Graph getSchedule incomplète.")
        entry = next(
            (
                v for v in values
                if isinstance(v, dict)
                and str(v.get("scheduleId") or "").strip().lower() == upn_lower
            ),
            values[0],
        )
        if not isinstance(entry, dict) or entry.get("error"):
            raise GraphError("Échec de lecture des disponibilités Graph.")
        for item in entry.get("scheduleItems") or []:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status") or "").strip().lower()
            if status == "free":
                continue
            s_utc = _parse_schedule_dt(item.get("start"))
            e_utc = _parse_schedule_dt(item.get("end"))
            if e_utc > s_utc:
                intervals.append((s_utc, e_utc))
        cursor = chunk_end

    intervals.sort(key=lambda pair: (pair[0], pair[1]))
    return intervals


def creer_reunion_teams(
    *,
    sujet: str,
    debut_utc: datetime,
    fin_utc: datetime,
    ordre_du_jour: str,
    courriels_invites: tuple[str, ...] = (),
    points_discussion: str = "",
    transaction_id: str = "",
) -> dict[str, Any]:
    """Create an Outlook/Teams calendar event and apply Teams meeting options."""
    if not Config.teams_configured():
        raise GraphNotConfigured(
            "L'intégration Microsoft Teams n'est pas configurée."
        )

    upn = Config.BOOKINGS_JURISTE_UPN.strip()
    encoded_upn = quote(upn, safe="")

    payload: dict[str, Any] = {
        "subject": sujet,
        "body": {
            "contentType": "HTML",
            "content": construire_corps_html_reunion(
                ordre_du_jour, points_discussion=points_discussion
            ),
        },
        "start": _utc_graph_dt(debut_utc),
        "end": _utc_graph_dt(fin_utc),
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        # Category prevents taches_bookings from re-importing; NO MIROIR_PROP_ID
        # so taches_outlook never treats this real event as a silent mirror.
        "categories": [MIROIR_CATEGORIE],
    }
    invites = [str(c).strip() for c in courriels_invites if str(c).strip()]
    if invites:
        payload["attendees"] = [
            {"emailAddress": {"address": addr}, "type": "required"}
            for addr in invites
        ]
    if transaction_id:
        payload["transactionId"] = transaction_id

    ev = graph.graph_post(f"/users/{encoded_upn}/events", payload) or {}
    graph_event_id = str(ev.get("id") or "").strip()
    graph_ical_uid = str(ev.get("iCalUId") or "").strip()
    online_meta = ev.get("onlineMeeting") or {}
    join_url = str(
        online_meta.get("joinUrl") or online_meta.get("joinWebUrl") or ""
    ).strip()

    if not graph_event_id or not join_url:
        raise GraphError(
            "La réponse Graph ne contient ni identifiant d'événement ni lien Teams."
        )

    online_meeting_id = ""
    options_appliquees = False
    try:
        user_id = Config.BOOKINGS_JURISTE_OBJECT_ID.strip()
        escaped_join = join_url.replace("'", "''")
        om_list = graph.graph_get(
            f"/users/{quote(user_id, safe='')}/onlineMeetings",
            params={"$filter": f"JoinWebUrl eq '{escaped_join}'"},
        )
        items = (om_list or {}).get("value") or []
        if items and isinstance(items[0], dict):
            online_meeting_id = str(items[0].get("id") or "").strip()
        if online_meeting_id:
            graph.graph_patch(
                f"/users/{quote(user_id, safe='')}/onlineMeetings/"
                f"{quote(online_meeting_id, safe='')}",
                {
                    "allowRecording": False,
                    "allowTranscription": True,
                    "recordAutomatically": False,
                    "meetingSpokenLanguageTag": "fr-ca",
                    "lobbyBypassSettings": {
                        "scope": "organizer",
                        "isDialInBypassEnabled": False,
                    },
                },
            )
            options_appliquees = True
    except GraphError:
        # Step 1 already committed the Outlook/Teams invite; return cleanly with
        # options_appliquees=False so the caller informs the lawyer honestly.
        pass

    return {
        "graph_event_id": graph_event_id,
        "graph_ical_uid": graph_ical_uid,
        "join_url": join_url,
        "online_meeting_id": online_meeting_id,
        "options_appliquees": options_appliquees,
    }


def modifier_reunion_teams(
    graph_event_id: str,
    *,
    sujet: str,
    debut_utc: datetime,
    fin_utc: datetime,
) -> None:
    """Update the subject and slot of an existing Outlook/Teams event."""
    if not Config.teams_configured():
        raise GraphNotConfigured(
            "L'intégration Microsoft Teams n'est pas configurée."
        )
    encoded_upn = quote(Config.BOOKINGS_JURISTE_UPN.strip(), safe="")
    encoded_id = quote(graph_event_id.strip(), safe="")
    graph.graph_patch(
        f"/users/{encoded_upn}/events/{encoded_id}",
        {
            "subject": sujet,
            "start": _utc_graph_dt(debut_utc),
            "end": _utc_graph_dt(fin_utc),
        },
    )


def annuler_reunion_teams(graph_event_id: str, motif: str = "") -> None:
    """Cancel an Outlook/Teams event (notifying attendees); 404 is a no-op."""
    if not Config.teams_configured():
        raise GraphNotConfigured(
            "L'intégration Microsoft Teams n'est pas configurée."
        )
    encoded_upn = quote(Config.BOOKINGS_JURISTE_UPN.strip(), safe="")
    encoded_id = quote(graph_event_id.strip(), safe="")
    try:
        graph.graph_post(
            f"/users/{encoded_upn}/events/{encoded_id}/cancel",
            {"Comment": motif or "Rendez-vous annulé."},
        )
    except GraphError as exc:
        if _is_not_found(exc):
            return
        raise


def supprimer_reunion_teams(graph_event_id: str) -> None:
    """Delete an Outlook/Teams event; 404 is treated as already deleted."""
    if not Config.teams_configured():
        raise GraphNotConfigured(
            "L'intégration Microsoft Teams n'est pas configurée."
        )
    encoded_upn = quote(Config.BOOKINGS_JURISTE_UPN.strip(), safe="")
    encoded_id = quote(graph_event_id.strip(), safe="")
    try:
        graph.graph_delete(f"/users/{encoded_upn}/events/{encoded_id}")
    except GraphError as exc:
        if _is_not_found(exc):
            return
        raise


def _decode_jwt_roles(token: str) -> list[str]:
    parts = (token or "").split(".")
    if len(parts) < 2 or not parts[1]:
        return []
    segment = parts[1]
    padded = segment + ("=" * ((-len(segment)) % 4))
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return []
    if not isinstance(claims, dict):
        return []
    raw_roles = claims.get("roles")
    if not isinstance(raw_roles, list):
        return []
    return sorted({str(r) for r in raw_roles if isinstance(r, str) and r.strip()})


def verifier_connexion_teams() -> dict[str, Any]:
    """Run read-only Teams, transcript policy, and OneDrive diagnostic checks."""
    interrupteur_actif = bool(Config.TEAMS_REUNIONS_ACTIF)
    configure = Config.teams_configured()
    result: dict[str, Any] = {
        "interrupteur_actif": interrupteur_actif,
        "configure": configure,
        "jeton_valide": False,
        "roles_presents": [],
        "roles_manquants": list(REQUIRED_GRAPH_ROLES),
        "utilisateur_accessible": False,
        "online_meetings_accessible": False,
        "politique_teams_requise": False,
        "transcriptions_accessibles": False,
        "onedrive_configure": False,
        "onedrive_accessible": False,
        "erreurs": [],
    }
    if not configure:
        result["erreurs"].append(
            "Les variables Microsoft Graph, BOOKINGS_JURISTE_UPN ou "
            "BOOKINGS_JURISTE_OBJECT_ID ne sont pas configurées."
        )
        return result

    try:
        token = graph.jeton_application()
    except GraphError as exc:
        result["erreurs"].append(str(exc))
        return result

    result["jeton_valide"] = True
    roles_presents = _decode_jwt_roles(token)
    result["roles_presents"] = roles_presents
    result["roles_manquants"] = [
        role for role in REQUIRED_GRAPH_ROLES if role not in roles_presents
    ]
    if result["roles_manquants"]:
        result["erreurs"].append(
            "Permissions Entra manquantes dans le jeton : "
            + ", ".join(result["roles_manquants"])
            + "."
        )

    encoded_upn = quote(Config.BOOKINGS_JURISTE_UPN.strip(), safe="")
    try:
        graph.graph_get(
            f"/users/{encoded_upn}/calendar",
            params={"$select": "id"},
        )
        result["utilisateur_accessible"] = True
    except GraphError as exc:
        result["erreurs"].append(f"Lecture du calendrier Graph : {exc}")

    user_id = Config.BOOKINGS_JURISTE_OBJECT_ID.strip()
    encoded_uid = quote(user_id, safe="")
    probe_url = _probe_join_url(Config.GRAPH_TENANT_ID.strip(), user_id)
    try:
        graph.graph_get(
            f"/users/{encoded_uid}/onlineMeetings",
            params={"$filter": f"JoinWebUrl eq '{probe_url}'"},
            follow_next=False,
        )
        result["online_meetings_accessible"] = True
    except GraphError as exc:
        msg = str(exc)
        if _is_not_found(exc):
            # HTTP 404 (meeting not found) proves Entra + CsApplicationAccessPolicy passed.
            result["online_meetings_accessible"] = True
        elif "HTTP 403" in msg:
            result["politique_teams_requise"] = True
            result["erreurs"].append(
                "Accès aux réunions Teams refusé : appliquez la politique "
                "CsApplicationAccessPolicy dans PowerShell (la propagation "
                "Microsoft prend de 30 min à 2 h)."
            )
        else:
            result["erreurs"].append(f"Sonde onlineMeetings : {msg}")

    try:
        graph.graph_get(
            f"/users/{encoded_uid}/onlineMeetings/"
            f"getAllTranscripts(meetingOrganizerUserId='{encoded_uid}')",
            params={"$top": 1},
            follow_next=False,
        )
        result["transcriptions_accessibles"] = True
    except GraphError as exc:
        result["erreurs"].append(
            f"Accès aux transcriptions Graph ({exc}) : vérifiez "
            "EnableGraphTranscriptAccess et la politique d'application Teams."
        )

    courrier_fn = getattr(Config, "courrier_configured", None)
    if callable(courrier_fn) and courrier_fn():
        result["onedrive_configure"] = True
        try:
            from utils import graph_courrier

            courrier_diag = graph_courrier.verifier_connexion()
            result["onedrive_accessible"] = bool(
                courrier_diag.get("onedrive_accessible")
            )
            if not result["onedrive_accessible"]:
                result["erreurs"].append(
                    "Dossier racine OneDrive inaccessible (module Courrier)."
                )
        except Exception as exc:
            result["erreurs"].append(
                f"Vérification OneDrive impossible ({type(exc).__name__})."
            )

    return result


def lister_transcriptions_reunion(online_meeting_id: str) -> list[dict[str, Any]]:
    """List Microsoft Teams call transcripts for *online_meeting_id*."""
    encoded_uid = _require_teams_user_id()
    mid = (online_meeting_id or "").strip()
    if not mid:
        raise GraphError("Identifiant de réunion Teams manquant.")
    encoded_mid = quote(mid, safe="")
    try:
        data = graph.graph_get(
            f"/users/{encoded_uid}/onlineMeetings/{encoded_mid}/transcripts"
        )
    except GraphError as exc:
        if _is_not_found(exc):
            return []
        raise
    items = (data or {}).get("value") if isinstance(data, dict) else []
    if not isinstance(items, list):
        raise GraphError("Réponse Graph transcripts invalide.")
    return [item for item in items if isinstance(item, dict)]


def lire_contenu_transcription_vtt(
    online_meeting_id: str,
    transcript_id: str,
    *,
    max_bytes: int = MAX_TRANSCRIPT_VTT_BYTES,
) -> bytes:
    """Download raw WebVTT bytes for one transcript up to *max_bytes*."""
    if max_bytes <= 0:
        raise ValueError("max_bytes doit être positif.")
    encoded_uid = _require_teams_user_id()
    mid = (online_meeting_id or "").strip()
    tid = (transcript_id or "").strip()
    if not mid or not tid:
        raise GraphError("Identifiant de réunion ou de transcription manquant.")
    url = (
        f"{graph._GRAPH_BASE}/users/{encoded_uid}/onlineMeetings/"
        f"{quote(mid, safe='')}/transcripts/{quote(tid, safe='')}/content"
    )
    response: Optional[requests.Response] = None
    try:
        try:
            token = graph.jeton_application()
            response = graph._session.get(
                url,
                params={"$format": "text/vtt"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/vtt",
                },
                timeout=graph._HTTP_TIMEOUT,
                stream=True,
            )
        except requests.RequestException as exc:
            raise GraphError(
                f"Échec réseau Graph ({type(exc).__name__})."
            ) from exc

        if response.status_code != 200:
            raise GraphError(
                f"Échec du téléchargement de la transcription Graph (HTTP {response.status_code})."
            )

        resp_headers = getattr(response, "headers", None) or {}
        raw_len = resp_headers.get("Content-Length")
        if raw_len is not None and str(raw_len).strip():
            try:
                declared_len = int(str(raw_len).strip())
            except ValueError:
                declared_len = None
            if declared_len is not None and declared_len > max_bytes:
                raise GraphError("Transcription Graph au-delà du plafond autorisé.")

        buf = bytearray()
        for chunk in response.iter_content(chunk_size=_VTT_CHUNK_BYTES):
            if not chunk:
                continue
            if len(buf) + len(chunk) > max_bytes:
                raise GraphError("Transcription Graph au-delà du plafond autorisé.")
            buf.extend(chunk)
        if not buf:
            raise GraphError("Transcription Graph reçue vide.")
        return bytes(buf)
    finally:
        if response is not None:
            close_fn = getattr(response, "close", None)
            if callable(close_fn):
                close_fn()


def lister_insights_reunion(
    online_meeting_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    """List Copilot ``aiInsights`` for *online_meeting_id*.

    Returns ``(insights, copilot_disponible)``. HTTP 400/403 returns
    ``([], False)`` so polling proceeds cleanly when Copilot notes are not
    enabled/licensed for the call.
    """
    encoded_uid = _require_teams_user_id()
    mid = (online_meeting_id or "").strip()
    if not mid:
        raise GraphError("Identifiant de réunion Teams manquant.")
    encoded_mid = quote(mid, safe="")
    try:
        data = graph.graph_get(
            f"/copilot/users/{encoded_uid}/onlineMeetings/{encoded_mid}/aiInsights"
        )
    except GraphError as exc:
        if _is_not_found(exc):
            return [], True
        if _is_copilot_unavailable(exc):
            return [], False
        raise
    items = (data or {}).get("value") if isinstance(data, dict) else []
    if not isinstance(items, list):
        raise GraphError("Réponse Graph aiInsights invalide.")
    return [item for item in items if isinstance(item, dict)], True


def lire_insight_reunion(
    online_meeting_id: str, insight_id: str
) -> Optional[dict[str, Any]]:
    """Fetch a single Copilot ``callAiInsight`` object, or None on HTTP 404."""
    encoded_uid = _require_teams_user_id()
    mid = (online_meeting_id or "").strip()
    iid = (insight_id or "").strip()
    if not mid or not iid:
        raise GraphError("Identifiant de réunion ou de résumé Copilot manquant.")
    encoded_mid = quote(mid, safe="")
    encoded_iid = quote(iid, safe="")
    try:
        data = graph.graph_get(
            f"/copilot/users/{encoded_uid}/onlineMeetings/{encoded_mid}/aiInsights/{encoded_iid}"
        )
    except GraphError as exc:
        if _is_not_found(exc) or _is_copilot_unavailable(exc):
            return None
        raise
    return data if isinstance(data, dict) else None


__all__ = [
    "MAX_TRANSCRIPT_VTT_BYTES",
    "REQUIRED_GRAPH_ROLES",
    "annuler_reunion_teams",
    "construire_corps_html_reunion",
    "creer_reunion_teams",
    "lire_contenu_transcription_vtt",
    "lire_insight_reunion",
    "lire_plages_occupees",
    "lister_insights_reunion",
    "lister_transcriptions_reunion",
    "modifier_reunion_teams",
    "powershell_setup_commands",
    "supprimer_reunion_teams",
    "verifier_connexion_teams",
]
