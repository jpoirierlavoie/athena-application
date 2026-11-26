"""Secondary sanitized read-only iCalendar feed (Lot F / 1j).

Publishes a subscription URL ``/calendrier-partage/<token>.ics`` disabled by
default until the lawyer generates a token in Paramètres → Intégrations. Only
the token's SHA-256 hash lives in Firestore (``settings/integrations``), verified
in constant time with ``hmac.compare_digest``.

Strict secrecy contract:
- Non-dossier ``PUBLIC`` hearings render normally.
- Dossier-linked hearings and ``PRIVATE`` / ``CONFIDENTIAL`` hearings render
  strictly as sanitized free/busy blocks (``SUMMARY:Occupé``,
  ``CLASS:CONFIDENTIAL``, ``TRANSP:OPAQUE``, UID/DTSTART/DTEND/DTSTAMP only —
  zero dossier numbers, client names, locations, descriptions, or URLs).
"""

import hashlib
from datetime import datetime, timedelta, timezone

import icalendar
from flask import Blueprint, Response

from models.hearing import _DAV_STATUS, _to_utc, list_hearings_range_strict
from models.integrations import verify_shared_calendar_token
from security import limiter
from tz import to_mtl
from utils import dav_classification, ical_tz
from utils.logging_setup import log_settings_event

calendrier_partage_bp = Blueprint("calendrier_partage", __name__)

_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 730
_PENDING_OR_REFUSED_CONFIRMATIONS = frozenset({
    "à_confirmer",
    "refusée",
    "annulée_client",
})


def _add_event_bounds(event: icalendar.Event, hearing: dict) -> bool:
    """Attach DTSTART and DTEND; returns True if timed (needs VTIMEZONE)."""
    start = hearing.get("start_datetime")
    end = hearing.get("end_datetime")
    if hearing.get("all_day"):
        if start and hasattr(start, "date"):
            debut = start.date()
            event.add("dtstart", debut)
            fin = (
                end.date()
                if end and hasattr(end, "date") and end.date() > debut
                else debut + timedelta(days=1)
            )
            event.add("dtend", fin)
        return False

    if start:
        event.add("dtstart", to_mtl(start))
    if end:
        event.add("dtend", to_mtl(end))
    elif start:
        event.add("dtend", to_mtl(_to_utc(start) + timedelta(hours=1)))
    return bool(start or end)


def _feed_uid(hearing: dict) -> str:
    uid = str(hearing.get("vevent_uid") or "").strip()
    if uid and not dav_classification.has_dossier_link(hearing):
        return uid
    if uid:
        # Avoid leaking non-UUID identifiers inside dossier-linked UIDs.
        if len(uid) == 36 and all(ch.isalnum() or ch == "-" for ch in uid):
            return uid
    seed = str(hearing.get("id") or hearing.get("vevent_uid") or "")
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return f"pallas-busy-{digest}"


def build_shared_calendar_ics(
    hearings: list[dict],
    *,
    now: datetime | None = None,
) -> str:
    """Render hearings into a sanitized read-only RFC 5545 VCALENDAR."""
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Pallas Athena//Calendrier Partage//FR")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "Agenda")
    cal.add("x-wr-timezone", ical_tz.TORONTO_TZID)

    stamp_fallback = _to_utc(now or datetime.now(timezone.utc))
    events: list[icalendar.Event] = []
    needs_tz = False

    for h in hearings:
        if not isinstance(h, dict):
            continue
        if h.get("status") == "annulée":
            continue
        if (h.get("confirmation") or "") in _PENDING_OR_REFUSED_CONFIRMATIONS:
            continue

        ev = icalendar.Event()
        ev.add("uid", _feed_uid(h))
        if _add_event_bounds(ev, h):
            needs_tz = True

        stamp = h.get("updated_at") or h.get("created_at") or stamp_fallback
        if stamp and hasattr(stamp, "hour"):
            ev.add("dtstamp", _to_utc(stamp))
        else:
            ev.add("dtstamp", stamp_fallback)

        is_dossier = dav_classification.has_dossier_link(h)
        cls = dav_classification.effective_classification(h)

        if (not is_dossier) and cls == dav_classification.CLASS_PUBLIC:
            ev.add("summary", h.get("title") or "")
            if h.get("location"):
                ev.add("location", h["location"])
            desc_parts = []
            if h.get("notes"):
                desc_parts.append(str(h["notes"]))
            if h.get("modalite") == "visioconférence" and h.get("conference_uri"):
                desc_parts.append(f"Visioconférence: {h['conference_uri']}")
            if desc_parts:
                ev.add("description", "\n".join(desc_parts))
            created = h.get("created_at")
            if created and hasattr(created, "hour"):
                ev.add("created", _to_utc(created))
            updated = h.get("updated_at")
            if updated and hasattr(updated, "hour"):
                ev.add("last-modified", _to_utc(updated))
            ev.add("status", _DAV_STATUS.get(h.get("status", ""), "TENTATIVE"))
            ev.add("class", dav_classification.CLASS_PUBLIC)
            ev.add("transp", "OPAQUE")
            ev.add("sequence", max(0, int(h.get("ical_sequence") or 0)))
        else:
            # Strict free/busy sanitization: no titles, clients, dossier
            # numbers, descriptions, locations, or conference URLs.
            ev.add("summary", "Occupé")
            ev.add("class", dav_classification.CLASS_CONFIDENTIAL)
            ev.add("transp", "OPAQUE")

        events.append(ev)

    if needs_tz:
        cal.add_component(ical_tz.build_america_toronto_vtimezone())
    for ev in events:
        cal.add_component(ev)
    return cal.to_ical().decode("utf-8")


@calendrier_partage_bp.route("/calendrier-partage/<token>.ics", methods=["GET"])
@limiter.limit("30 per minute")
def feed_ics(token: str) -> Response:
    """Serve the token-authenticated sanitized read-only iCalendar feed."""
    ok, reason = verify_shared_calendar_token(token)
    if not ok:
        log_settings_event("shared_calendar_rejected", reason=reason)
        if reason == "store_unreadable":
            resp = Response("Service Unavailable", status=503,
                            content_type="text/plain; charset=utf-8")
            resp.headers["Retry-After"] = "60"
            return resp
        return Response("Not Found", status=404,
                        content_type="text/plain; charset=utf-8")

    now = datetime.now(timezone.utc)
    floor_utc = now - timedelta(days=_LOOKBACK_DAYS)
    ceiling_utc = now + timedelta(days=_LOOKAHEAD_DAYS)
    try:
        hearings = list_hearings_range_strict(floor_utc, ceiling_utc)
    except Exception as exc:
        log_settings_event(
            "shared_calendar_rejected",
            reason="read_failed",
            error_type=type(exc).__name__,
        )
        resp = Response("Service Unavailable", status=503,
                        content_type="text/plain; charset=utf-8")
        resp.headers["Retry-After"] = "60"
        return resp

    ics_body = build_shared_calendar_ics(hearings, now=now)
    return Response(
        ics_body,
        status=200,
        content_type="text/calendar; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
