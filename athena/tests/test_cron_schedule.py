"""cron.yaml, utils/cron_schedule and the texts that promise a delay.

The lawyer's decision of 2026-11-12: the three cron jobs (portal
reconciliation, Bookings sync, Outlook mirror) run during office hours only,
from 9 h to 17 h Montréal time, instead of around the clock — their runs keep
the instance warm while he works, and it sleeps the rest of the time.
``cron.yaml`` (repository root) is what App Engine runs;
``utils.cron_schedule`` is what the application's texts quote — to the lawyer
in Réception, to Claude through the connector. One changed without the other
promises a delay the platform does not keep.
"""

import os
import pathlib
import sys
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import pytest

ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from flask import Flask, render_template  # noqa: E402

with mock.patch("google.cloud.firestore.Client"):
    import routes.reception as reception
    from mcp import handlers, tools
    from services import rendez_vous

from tz import MTL, to_mtl  # noqa: E402
from utils import cron_schedule  # noqa: E402
from utils.icons import ms  # noqa: E402

CRON_YAML = ATHENA.parent / "cron.yaml"


def _entries() -> list[dict]:
    """The ``cron:`` entries of cron.yaml, each a flat ``key: value`` map
    (the file holds nothing deeper; no yaml dependency for one table)."""
    entries: list[dict] = []
    for raw in CRON_YAML.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line == "cron:":
            continue
        if line.startswith("- "):
            entries.append({})
            line = line[2:]
        key, _, value = line.partition(":")
        entries[-1][key.strip()] = value.strip()
    return entries


def test_each_job_runs_during_office_hours_at_the_cadence_the_texts_quote():
    entries = _entries()
    by_url = {e["url"]: e for e in entries}
    assert len(by_url) == len(entries) == (
        len(cron_schedule.JOBS) + len(cron_schedule.DAILY_JOBS))
    for job, (url, _minutes, _offset) in cron_schedule.JOBS.items():
        entry = by_url[url]
        assert entry["schedule"] == cron_schedule.cadence_en(job), job
        assert entry.get("timezone") == "America/Toronto", job
        assert entry.get("target") == "default", job
    # Rolling offsets guarantee no two jobs share the same start minute (00/05/10/15).
    offsets = [cron_schedule.JOBS[job][2] % cron_schedule.JOBS[job][1] for job in cron_schedule.JOBS]
    assert len(set(offsets)) == len(offsets)
    assert all(o != cron_schedule.daily_time().minute for o in (offset for _, _, offset in cron_schedule.JOBS.values()))
    # The daily jobs run once, with the first pass (the invoice lateness
    # sweep, the lawyer's decision of 2026-11-12).
    for job, url in cron_schedule.DAILY_JOBS.items():
        entry = by_url[url]
        assert entry["schedule"] == cron_schedule.daily_en(job), job
        assert entry.get("timezone") == "America/Toronto", job
        assert entry.get("target") == "default", job


def test_the_cron_time_zone_is_montreals_daylight_saving_included():
    toronto = ZoneInfo("America/Toronto")
    for month in (1, 3, 4, 7, 11, 12):
        noon = datetime(2031, month, 15, 12)
        assert noon.replace(tzinfo=toronto).utcoffset() == noon.replace(tzinfo=MTL).utcoffset()


def test_the_cadences_read_in_french_and_in_english():
    assert cron_schedule.cadence_fr("bookings") == "toutes les 15 minutes, de 9 h à 17 h"
    assert cron_schedule.cadence_fr("portail") == "toutes les 15 minutes, de 9 h à 17 h"
    assert cron_schedule.cadence_en("bookings") == "every 15 minutes from 09:05 to 17:05"
    assert cron_schedule.cadence_en("outlook") == "every 15 minutes from 09:10 to 17:10"
    assert cron_schedule.cadence_en("portail") == "every 15 minutes from 09:15 to 17:15"
    assert cron_schedule.daily_en("factures") == "every day 09:00"
    assert f"{cron_schedule.daily_time():%H:%M}" == "09:00"
    with pytest.raises(KeyError):
        cron_schedule.daily_en("portail")


@pytest.mark.parametrize("text, quoted", [
    (rendez_vous.REFUS_NON_INSCRIT, cron_schedule.cadence_fr("bookings")),
    (handlers._HEARING_CANCELLED, cron_schedule.cadence_fr("outlook")),
    (handlers._REFUS_NON_INSCRIT_MCP, cron_schedule.cadence_fr("bookings")),
    (tools.TOOLS["update_hearing"]["description"], cron_schedule.cadence_en("outlook")),
], ids=["reception-refus", "mcp-annulation", "mcp-refus", "mcp-update_hearing"])
def test_every_text_that_promises_a_delay_quotes_the_cadence(text, quoted):
    assert quoted in text
    assert "au plus" not in text and "10-minute" not in text


@pytest.mark.parametrize("read_fails", [False, True])
def test_the_reception_footnote_quotes_the_cadence(monkeypatch, read_fails):
    def lister():
        if read_fails:
            raise rendez_vous.LectureImpossible("test")
        return []

    monkeypatch.setattr(rendez_vous, "lister_en_attente", lister)
    monkeypatch.setattr(rendez_vous, "lister_divergences", lambda: [])
    app = Flask(__name__, template_folder=str(ATHENA / "templates"))
    app.jinja_env.globals.update(csrf_token=lambda: "tok", ms=ms)
    app.jinja_env.filters["to_mtl"] = to_mtl
    app.register_blueprint(reception.reception_bp)
    with app.test_request_context():
        html = render_template("reception/_rdv.html", feature_intake=True,
                               **reception._contexte_rdv())
    flat = " ".join(html.split())
    assert (f"importés automatiquement {cron_schedule.cadence_fr('bookings')}."
            in flat)


def test_no_source_still_promises_the_old_cadence():
    """The promise « 10 minutes au plus » outlived the cron it described in
    five places, and night runs no longer exist; none may come back."""
    stale = ("10 minutes au plus", "10 min au plus", "10-minute cycle",
             "10-minute mirror")
    offenders = []
    for path in ATHENA.rglob("*"):
        if path.suffix not in {".py", ".html", ".md"} or "tests" in path.parts:
            continue
        if any(part in {"venv", ".venv", "node_modules"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        offenders += [f"{path.relative_to(ATHENA)}: {s}" for s in stale if s in text]
    assert offenders == []
