"""Timezone helpers for Pallas Athena.

All Firestore timestamps are stored as UTC.  The single user works from
Montréal, so every datetime shown in the UI or serialised to CalDAV must
be converted to Eastern Time (``America/Toronto``, the canonical IANA zone
for Montréal — ``America/Montreal`` is a deprecated backward alias).
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

EASTERN_TZID = "America/Toronto"
MTL = ZoneInfo(EASTERN_TZID)
TORONTO = MTL


def to_mtl(dt: datetime | None) -> datetime | None:
    """Convert a UTC (or naive) datetime to Eastern Time (Montréal).

    Returns *None* unchanged so Jinja2 filters can be applied
    unconditionally.
    """
    if dt is None:
        return None
    if not isinstance(dt, datetime):
        return dt  # date-only objects — nothing to shift
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(MTL)


to_toronto = to_mtl


def mtl_to_utc(dt: datetime | None) -> datetime | None:
    """Interpret a naive datetime as Montreal local time and convert to UTC.

    Used when parsing HTML form inputs, which carry no timezone but are
    entered by the user in Montreal time.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MTL)
    return dt.astimezone(timezone.utc)


__all__ = [
    "EASTERN_TZID",
    "MTL",
    "TORONTO",
    "to_mtl",
    "to_toronto",
    "mtl_to_utc",
]
