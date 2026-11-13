"""Dashboard route — Phase 11: at-a-glance summary after login."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, render_template

from auth import login_required
from utils import deadlines
from tz import MTL

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint("dashboard", __name__)

_DASHBOARD_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="dash")
_STATS_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dash-stats")

# The « Factures impayées » tile's links: one per issued status — the pair
# invoice.get_outstanding_totals sums (its _ISSUED_STATUSES, pinned equal by
# tests/test_dashboard_invoice_tile.py), in that order — each to that
# status' own list. Plural, under the tile's « Factures impayées ».
_OUTSTANDING_LINK_LABELS = {"envoyée": "Envoyées", "en_retard": "En retard"}


@dashboard_bp.route("/")
@login_required
def index() -> str:
    """Render the main dashboard with schedule, urgent items, and stats."""
    now = datetime.now(timezone.utc)
    today = now.astimezone(MTL).date()

    # ── Fetch independent dashboard sections concurrently ─────────────
    f_short_term = _DASHBOARD_POOL.submit(_get_hearings_from_day, today, now + timedelta(days=7))
    f_long_term = _DASHBOARD_POOL.submit(_get_hearings_in_range, now + timedelta(days=7), now + timedelta(days=60))
    f_urgent_tasks = _DASHBOARD_POOL.submit(_get_urgent_tasks, now)
    f_urgent_steps = _DASHBOARD_POOL.submit(_get_urgent_protocol_steps, now)
    f_prescription = _DASHBOARD_POOL.submit(_get_prescription_alerts, now)
    f_stats = _DASHBOARD_POOL.submit(_get_quick_stats)

    short_term_hearings = f_short_term.result()
    long_term_hearings = f_long_term.result()
    urgent_tasks = f_urgent_tasks.result()
    urgent_protocol_steps = f_urgent_steps.result()
    prescription_alerts = f_prescription.result()
    stats = f_stats.result()

    return render_template(
        "dashboard/index.html",
        short_term_hearings=short_term_hearings,
        long_term_hearings=long_term_hearings,
        urgent_tasks=urgent_tasks,
        urgent_protocol_steps=urgent_protocol_steps,
        prescription_alerts=prescription_alerts,
        stats=stats,
        now=now,
    )


# ── Data helpers ─────────────────────────────────────────────────────────


def _get_hearings_in_range(date_from: datetime, date_to: datetime) -> list[dict]:
    """Return non-cancelled hearings in a date range, chronologically.

    The date range, ordering, and bound (100 rows) run server-side via
    ``list_hearings_in_range``; only the cancelled-status exclusion stays
    in Python, over the already-small result.
    """
    try:
        from models.hearing import list_hearings_in_range
        hearings = list_hearings_in_range(date_from, date_to)
        return [
            h for h in hearings
            if h.get("status") not in ("annulée",)
        ]
    except Exception:
        return []


def _get_hearings_from_day(day: date, date_to: datetime) -> list[dict]:
    """Non-cancelled hearings whose CIVIL day is *day* or later, starting no
    later than *date_to*, chronologically.

    The read opens at ``civil_day_floor(day)`` — midnight UTC, the earlier
    of the day's two starts (an all-day hearing's) — and every row is then
    judged by ``occurrence_day``, the one rule for a hearing's civil day
    (the MCP ``get_agenda`` and the dossier « Calendrier » tab read the same
    way). Today's timed hearings stay listed for the whole day, as they do
    on those two surfaces.
    """
    try:
        from models.hearing import (
            civil_day_floor,
            list_hearings_in_range,
            on_or_after_day,
        )
        hearings = on_or_after_day(
            list_hearings_in_range(civil_day_floor(day), date_to), day
        )
        return [
            h for h in hearings
            if h.get("status") not in ("annulée",)
        ]
    except Exception:
        return []


def _get_urgent_tasks(now: datetime) -> list[dict]:
    """Return tasks due within 14 days or overdue (not completed/cancelled).

    The status/due-date filters, ordering, and bound run server-side via
    ``list_urgent_tasks`` (tasks without a due_date are excluded by the
    range filter, matching the previous behaviour); the overdue-first
    re-sort happens here over the bounded result.
    """
    try:
        from models.task import list_urgent_tasks
        cutoff = now + timedelta(days=14)
        urgent = list_urgent_tasks(cutoff)
        # ONE rule on every surface (2026-08-02): Montréal day, prorogued
        # deadline. The old wall-clock compare flipped a task to overdue at
        # 00:00 UTC — 20:00 the previous evening here — which is how three
        # tasks due Monday read « en retard » on a Sunday night.
        today = now.astimezone(MTL).date()
        for t in urgent:
            t["_overdue"] = deadlines.is_past_due(t.get("due_date"), today=today)
            t["_days_left"] = deadlines.days_until(t.get("due_date"), today=today)
        # Overdue first, then by due_date ascending
        urgent.sort(key=lambda t: (not t.get("_overdue", False), t.get("due_date") or now))
        return urgent
    except Exception:
        return []


def _get_urgent_protocol_steps(now: datetime) -> list[dict]:
    """Return protocol steps due within 14 days or overdue.

    One collection-group query plus one batched parent-protocol fetch
    (``list_urgent_steps``) replaces the previous per-protocol N+1 scan.
    The model attaches ``_protocol_title`` / ``_protocol_id`` /
    ``_dossier_file_number``; the overdue flag and re-sort happen here.
    """
    try:
        from models.protocol import list_urgent_steps
        cutoff = now + timedelta(days=14)
        urgent_steps = list_urgent_steps(cutoff)
        # Montréal day + prorogation — the same predicate as everywhere.
        today = now.astimezone(MTL).date()
        for step in urgent_steps:
            deadline = step.get("deadline_date")
            step["_overdue"] = deadlines.is_past_due(deadline, today=today)
            step["_days_left"] = deadlines.days_until(deadline, today=today)

        urgent_steps.sort(
            key=lambda s: (not s.get("_overdue", False), s.get("deadline_date") or now)
        )
        return urgent_steps
    except Exception:
        return []


def _get_prescription_alerts(now: datetime) -> list[dict]:
    """Return dossiers with prescription dates within 60 days.

    Both filters (status actif OR en_attente — one query per status —
    and prescription_date <= cutoff) run server-side, bounded, via
    ``list_prescription_alerts``; the juridical-day computation stays here.
    """
    try:
        from models.dossier import list_prescription_alerts
        from utils.deadlines import last_action_day

        cutoff = now + timedelta(days=60)
        alerts = []
        for d in list_prescription_alerts(cutoff):
            # The EFFECTIVE date (WP13: events may have pushed it past the
            # raw one); falls back to the raw date for event-less dossiers.
            pdate = d.get("prescription_date_effective") or d.get(
                "prescription_date"
            )
            if not pdate:
                continue  # a_verifier row with no computable date
            # Calendar days on the Montréal day — the wall-clock delta
            # under-counted by one for most of each day. CIVIL calendar:
            # the date is a prescription date (art. 2879 C.c.Q.).
            d["_days_remaining"] = max(
                0,
                deadlines.days_until(
                    pdate,
                    today=now.astimezone(MTL).date(),
                    regime=deadlines.CIVIL,
                )
                or 0,
            )
            pdate_as_date = pdate.date() if hasattr(pdate, "date") else pdate
            # PROCEDURAL, on purpose: the last day the demande can be FILED
            # (art. 2892 C.c.Q.; art. 82 C.p.c.) — see last_action_day.
            last_action, differs = last_action_day(pdate_as_date)
            d["_last_action_date"] = last_action
            d["_last_action_differs"] = differs
            alerts.append(d)
        alerts.sort(
            key=lambda d: d.get("prescription_date_effective")
            or d.get("prescription_date")
            or now
        )
        return alerts
    except Exception:
        return []


def _get_quick_stats() -> dict:
    """Return counts and amounts for the dashboard stats cards.

    The counts and the unbilled time are server-side aggregations (COUNT/
    SUM); the outstanding invoices are a Python sum of their live balances
    (``invoice.get_outstanding_totals``, which says why). Each stat degrades
    independently: a failure leaves its safe default in place.
    ``outstanding_links`` — one ``{status, label, amount}`` per issued
    status — always holds the tile's links; ``outstanding_unavailable``
    says the sums failed, and the tile then shows « indisponible » instead
    of any amount — never a 0 that reads « nothing is owed ».
    """
    stats: dict = {
        "open_dossiers": 0,
        "unbilled_hours": 0.0,
        "unbilled_amount": 0,
        "outstanding_invoices": 0,
        "trust_total": 0,
        "trust_reconciliation_overdue": False,
    }
    def _load_open():
        from models.dossier import count_open
        return count_open()

    def _load_unbilled():
        from models.time_entry import get_unbilled_totals
        return get_unbilled_totals()

    def _load_outstanding():
        from models.invoice import get_outstanding_totals_strict
        return get_outstanding_totals_strict()

    def _load_trust():
        from models import trust
        return trust.get_firm_trust_snapshot()

    f_open = _STATS_POOL.submit(_load_open)
    f_unbilled = _STATS_POOL.submit(_load_unbilled)
    f_inv = _STATS_POOL.submit(_load_outstanding)
    f_trust = _STATS_POOL.submit(_load_trust)

    try:
        stats["open_dossiers"] = f_open.result()
    except Exception as exc:
        logger.warning("dashboard: open dossiers stat failed: %s", exc)

    try:
        totals = f_unbilled.result()
        stats["unbilled_hours"] = totals.get("hours", 0.0)
        stats["unbilled_amount"] = totals.get("amount", 0)
    except Exception as exc:
        logger.warning("dashboard: unbilled time stat failed: %s", exc)

    by_status: dict = {}
    stats["outstanding_unavailable"] = False
    try:
        # ONE call for the whole tile: its total and each link's own figure
        # are the same sums, so they never disagree — and the total is what
        # get_outstanding_total gives the connector's get_agenda.
        by_status = f_inv.result()
        stats["outstanding_invoices"] = sum(by_status.values())
    except Exception as exc:
        # A failed read never passes for « nothing is owed »: the tile says
        # « indisponible » (the lawyer's choice of 2026-11-11). The type
        # only: an exception's text may quote a stored value.
        stats["outstanding_unavailable"] = True
        logger.warning("dashboard: outstanding invoices stat failed: %s",
                       type(exc).__name__)
    # The links stand even when the sums failed: the way to the lists.
    stats["outstanding_links"] = [
        {"status": status, "label": label, "amount": by_status.get(status, 0)}
        for status, label in _OUTSTANDING_LINK_LABELS.items()
    ]

    try:
        snapshot = f_trust.result()
        stats["trust_total"] = snapshot.get("total_held_cents", 0)
        stats["trust_reconciliation_overdue"] = snapshot.get("reconciliation_overdue", False)
    except Exception as exc:
        logger.warning("dashboard: trust stat failed: %s", exc)

    return stats
