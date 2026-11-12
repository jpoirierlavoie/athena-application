"""When the cron jobs run — the one source the application's texts quote.

``cron.yaml`` (repository root, deployed by Cloud Build on every push) runs
the portal reconciliation, the Bookings sync and the Outlook mirror at these
Montréal hours, once each — the lawyer's decision of 2026-11-12: three runs a
day, so the instance can sleep between them instead of being woken every 10
to 15 minutes. ``tests/test_cron_schedule.py`` pins ``cron.yaml`` to
:data:`CRON_HOURS`, and the texts that promise a delay to this module.
"""

#: Montréal hours of the three daily runs, in order.
CRON_HOURS = (6, 12, 18)


def _joined(labels: list, conjunction: str) -> str:
    return f"{', '.join(labels[:-1])} {conjunction} {labels[-1]}"


def runs_fr(conjunction: str = "et") -> str:
    """The run hours in French: « 6 h, 12 h et 18 h » (or « … ou 18 h »)."""
    return _joined([f"{hour} h" for hour in CRON_HOURS], conjunction)


def runs_24h(conjunction: str = "and") -> str:
    """The run hours for the connector's English texts: « 06:00, 12:00 and
    18:00 »."""
    return _joined([f"{hour:02d}:00" for hour in CRON_HOURS], conjunction)
