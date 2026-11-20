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
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from config import Config
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

_PROBE_JOIN_URL = "https://teams.microsoft.com/l/meetup-join/pallas-probe"


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


def _resoudre_user_id(upn: str) -> str:
    """Resolve the Entra user object ID for *upn* (raises GraphError when absent)."""
    encoded_upn = quote((upn or "").strip(), safe="")
    res = graph.graph_get(f"/users/{encoded_upn}", params={"$select": "id"})
    user_id = str((res or {}).get("id") or "").strip()
    if not user_id:
        raise GraphError("Identifiant utilisateur Graph manquant.")
    return user_id


def _is_not_found(exc: GraphError) -> bool:
    return "HTTP 404" in str(exc)


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
        user_id = _resoudre_user_id(upn)
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
                    "allowTranscription": True,
                    "recordAutomatically": False,
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
        "onedrive_accessible": False,
        "erreurs": [],
    }
    if not configure:
        result["erreurs"].append(
            "Les variables Microsoft Graph ou BOOKINGS_JURISTE_UPN ne sont pas configurées."
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

    user_id = ""
    try:
        user_id = _resoudre_user_id(Config.BOOKINGS_JURISTE_UPN)
        result["utilisateur_accessible"] = True
    except GraphError as exc:
        result["erreurs"].append(f"Lecture de l'utilisateur Graph : {exc}")

    if user_id:
        encoded_uid = quote(user_id, safe="")
        try:
            graph.graph_get(
                f"/users/{encoded_uid}/onlineMeetings",
                params={"$filter": f"JoinWebUrl eq '{_PROBE_JOIN_URL}'"},
            )
            result["online_meetings_accessible"] = True
        except GraphError as exc:
            msg = str(exc)
            if "HTTP 403" in msg or "HTTP 404" in msg:
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
    else:
        result["erreurs"].append(
            "Le module Courrier (OneDrive) n'est pas configuré."
        )

    return result


__all__ = [
    "REQUIRED_GRAPH_ROLES",
    "annuler_reunion_teams",
    "construire_corps_html_reunion",
    "creer_reunion_teams",
    "modifier_reunion_teams",
    "powershell_setup_commands",
    "supprimer_reunion_teams",
    "verifier_connexion_teams",
]
