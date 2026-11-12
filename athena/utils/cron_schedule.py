"""When the cron jobs run — the one source the application's texts quote.

``cron.yaml`` (repository root, deployed by Cloud Build on every push) runs
the portal reconciliation, the Bookings sync and the Outlook mirror during
office hours only — the lawyer's decision of 2026-11-12. Their runs keep the
instance warm from 9 h to 17 h (an instance stays up about 15 minutes after
its last request); outside those hours only real use wakes it. App Engine's
cron syntax cannot limit an interval to weekdays, so weekends keep the same
hours. ``tests/test_cron_schedule.py`` pins ``cron.yaml`` to this module, and
the texts that promise a delay to it.
"""

#: Montréal hours of the window: its first run, and its last run's bound.
OFFICE_HOURS = (9, 17)

#: Each job: its URL, and the minutes between two of its runs.
JOBS = {
    "portail": ("/taches/portail/reconciliation", 15),
    "bookings": ("/taches/bookings/sync", 10),
    "outlook": ("/taches/outlook/sync", 10),
}


def cadence_fr(job: str) -> str:
    """« toutes les 10 minutes, de 9 h à 17 h » — for the French texts."""
    start, end = OFFICE_HOURS
    return f"toutes les {JOBS[job][1]} minutes, de {start} h à {end} h"


def cadence_en(job: str) -> str:
    """« every 10 minutes from 09:00 to 17:00 » — the job's ``cron.yaml``
    schedule, word for word, and the connector's English texts."""
    start, end = OFFICE_HOURS
    return f"every {JOBS[job][1]} minutes from {start:02d}:00 to {end:02d}:00"
