"""cron.yaml, utils/cron_schedule and the texts that promise a delay.

The lawyer's decision of 2026-11-12: the three cron jobs (portal
reconciliation, Bookings sync, Outlook mirror) run at 6 h, 12 h and 18 h,
Montréal time, instead of every 10 to 15 minutes, so the instance can sleep
between uses. ``cron.yaml`` (repository root) is what App Engine runs;
``utils.cron_schedule.CRON_HOURS`` is what the application's texts quote — to
the lawyer in Réception, to Claude through the connector. One changed without
the other promises a delay the platform does not keep.
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
JOBS = ("/taches/portail/reconciliation", "/taches/bookings/sync",
        "/taches/outlook/sync")


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


def test_each_job_runs_exactly_at_the_hours_the_texts_quote():
    entries = _entries()
    assert sorted({e["url"] for e in entries}) == sorted(JOBS)
    expected = sorted(f"every day {hour:02d}:00" for hour in cron_schedule.CRON_HOURS)
    for url in JOBS:
        mine = [e for e in entries if e["url"] == url]
        assert sorted(e["schedule"] for e in mine) == expected, url
        assert {e.get("timezone") for e in mine} == {"America/Toronto"}, url
        assert {e.get("target") for e in mine} == {"default"}, url
    assert len(entries) == len(JOBS) * len(cron_schedule.CRON_HOURS)
    assert len({e["description"] for e in entries}) == len(entries)


def test_the_cron_time_zone_is_montreals_daylight_saving_included():
    toronto = ZoneInfo("America/Toronto")
    for month in (1, 3, 4, 7, 11, 12):
        noon = datetime(2031, month, 15, 12)
        assert noon.replace(tzinfo=toronto).utcoffset() == noon.replace(tzinfo=MTL).utcoffset()


def test_the_hours_read_in_french_and_in_24_hour_form():
    assert cron_schedule.runs_fr() == "6 h, 12 h et 18 h"
    assert cron_schedule.runs_fr("ou") == "6 h, 12 h ou 18 h"
    assert cron_schedule.runs_24h("or") == "06:00, 12:00 or 18:00"


@pytest.mark.parametrize("text, quoted", [
    (rendez_vous.REFUS_NON_INSCRIT, cron_schedule.runs_fr("ou")),
    (handlers._HEARING_CANCELLED, cron_schedule.runs_fr("ou")),
    (handlers._REFUS_NON_INSCRIT_MCP, cron_schedule.runs_fr("ou")),
    (tools.TOOLS["update_hearing"]["description"], cron_schedule.runs_24h("or")),
], ids=["reception-refus", "mcp-annulation", "mcp-refus", "mcp-update_hearing"])
def test_every_text_that_promises_a_delay_quotes_the_hours(text, quoted):
    assert quoted in text
    assert "10 min" not in text and "10-minute" not in text


@pytest.mark.parametrize("read_fails", [False, True])
def test_the_reception_footnote_quotes_the_hours(monkeypatch, read_fails):
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
    assert f"importés automatiquement à {cron_schedule.runs_fr()}." in flat


def test_no_source_still_promises_the_old_cadence():
    """The promise « 10 minutes au plus » outlived the cron it described
    in five places; none may come back."""
    stale = ("10 minutes au plus", "10 min au plus", "toutes les 10 minutes",
             "10-minute cycle", "10-minute mirror", "every 10 minutes")
    offenders = []
    for path in ATHENA.rglob("*"):
        if path.suffix not in {".py", ".html", ".md"} or "tests" in path.parts:
            continue
        if any(part in {"venv", ".venv", "node_modules"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        offenders += [f"{path.relative_to(ATHENA)}: {s}" for s in stale if s in text]
    assert offenders == []
