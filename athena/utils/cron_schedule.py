"""When the cron jobs run — the one source the application's texts quote.

``cron.yaml`` (repository root, deployed by Cloud Build on every push) runs
its periodic jobs (:data:`JOBS`) during office hours only, and its daily
jobs (:data:`DAILY_JOBS`) once, with the first pass — the lawyer's
decisions of 2026-11-12. Their runs keep the
instance warm from 9 h to 17 h (an instance stays up about 15 minutes after
its last request); outside those hours only real use wakes it. App Engine's
cron syntax cannot limit an interval to weekdays, so weekends keep the same
hours. ``tests/test_cron_schedule.py`` pins ``cron.yaml`` to this module, and
the texts that promise a delay to it.
"""

from datetime import time

#: Montréal hours of the window: its first run, and its last run's bound.
OFFICE_HOURS = (9, 17)

#: Each job: its URL, the minutes between two of its runs, and the start-minute
#: offset (rolling 5-minute stagger so cron jobs never fire simultaneously).
JOBS = {
    "portail": ("/taches/portail/reconciliation", 15, 15),
    "bookings": ("/taches/bookings/sync", 15, 5),
    "outlook": ("/taches/outlook/sync", 15, 10),
}

#: Each job run ONCE a day, at the window's first hour: its URL. The
#: invoice lateness sweep runs with the day's first pass (the lawyer's
#: decision of 2026-11-12), so it wakes no sleeping instance.
DAILY_JOBS = {
    "factures": "/taches/factures/retards",
}


def cadence_fr(job: str) -> str:
    """« toutes les 15 minutes, de 9 h à 17 h » — for the French texts."""
    start, end = OFFICE_HOURS
    return f"toutes les {JOBS[job][1]} minutes, de {start} h à {end} h"


def cadence_en(job: str) -> str:
    """« every 15 minutes from 09:10 to 17:10 » — the job's ``cron.yaml``
    schedule, word for word, and the connector's English texts."""
    start, end = OFFICE_HOURS
    _url, minutes, offset = JOBS[job]
    return f"every {minutes} minutes from {start:02d}:{offset:02d} to {end:02d}:{offset:02d}"


def daily_time() -> time:
    """The Montréal time of the daily jobs: the window's first hour."""
    return time(OFFICE_HOURS[0], 0)


def daily_en(job: str) -> str:
    """« every day 09:00 » — a daily job's ``cron.yaml`` schedule, word for
    word."""
    if job not in DAILY_JOBS:
        raise KeyError(job)
    return f"every day {daily_time():%H:%M}"
