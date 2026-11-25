"""iCalendar timezone & VALARM helpers shared by DAV serializers and feeds."""

from __future__ import annotations

from datetime import date

import icalendar

__all__ = [
    "TORONTO_TZID",
    "MAX_VALARMS",
    "MAX_VALARM_CHARS",
    "build_america_toronto_vtimezone",
    "parse_valarm_text",
    "serialize_valarm",
    "normalize_valarm_list",
]

TORONTO_TZID = "America/Toronto"

MAX_VALARMS = 10
MAX_VALARM_CHARS = 4096


def build_america_toronto_vtimezone() -> icalendar.Timezone:
    """Build a standard RFC 5545 VTIMEZONE for America/Toronto via zoneinfo."""
    return icalendar.Timezone.from_tzid(
        TORONTO_TZID,
        first_date=date(2007, 1, 1),
        last_date=date(2050, 1, 1),
    )


def parse_valarm_text(raw: object) -> icalendar.Alarm | None:
    """Parse one serialized VALARM block back into an icalendar.Alarm."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or len(text) > MAX_VALARM_CHARS:
        return None
    if not text.upper().startswith("BEGIN:VALARM"):
        return None
    wrapped = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Pallas//VALARM//FR\r\n"
        "BEGIN:VEVENT\r\nUID:valarm-parse\r\nDTSTAMP:20260101T000000Z\r\n"
        f"{text.replace(chr(13) + chr(10), chr(10)).replace(chr(10), chr(13) + chr(10))}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    try:
        cal = icalendar.Calendar.from_ical(wrapped)
    except Exception:
        return None
    for comp in cal.walk():
        if comp.name == "VALARM":
            # Ensure valid standalone alarm without nested subcomponents
            if getattr(comp, "subcomponents", None):
                return None
            return comp
    return None


def serialize_valarm(alarm: icalendar.Alarm) -> str | None:
    """Serialize an icalendar.Alarm safely (bounded, re-parseable)."""
    try:
        # Strip nested components if a malformed client sent one
        clean = icalendar.Alarm()
        for k, v in alarm.items():
            clean[k] = v
        raw = clean.to_ical().decode("utf-8").strip()
    except Exception:
        return None
    if not raw or len(raw) > MAX_VALARM_CHARS:
        return None
    if parse_valarm_text(raw) is None:
        return None
    return raw


def normalize_valarm_list(values: object) -> list[str]:
    """Validate and normalize a list of stored/incoming VALARM strings."""
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    for item in values[:MAX_VALARMS]:
        alarm = parse_valarm_text(item)
        if alarm is None:
            continue
        ser = serialize_valarm(alarm)
        if ser:
            out.append(ser)
    return out
