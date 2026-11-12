"""The nightly lateness sweep — ``routes/taches_factures`` (lot 2 of
« Factures : retard, radiation, intérêts », 2026-11-11).

Pins what fails silently in this machine route:

- the cron guard: 403 without ``X-Appengine-Cron: true``, before anything is
  read;
- the kill switch ``FACTURES_RETARD_ACTIF``: off, nothing read or written;
  read as ``MIROIR_OUTLOOK_ACTIF`` is, and set in app.yaml by ONE line that
  the runbook flips both ways (DEPLOYMENT.md §15) — its form is pinned,
  never its value;
- a failed or truncated read writes nothing and says so (200, an ERROR line);
- only what ``invoice.is_overdue`` says is late is marked, through
  ``update_status_report`` against the etag read — a changed invoice is
  refused, never overwritten — the longest-late first, at most
  ``OVERDUE_SWEEP_CAP`` a run, the rest counted;
- a second run is idempotent; every write is stamped « cron »; the one
  counter line carries the run's counts;
- ``/taches/factures/retards`` never collides with the web task list's
  ``/taches/<task_id>``, and the real app registers it CSRF-exempt;
- ``cron.yaml`` schedules it; the read-only preview
  (``scripts/apercu_factures_echues``) lists exactly what its NEXT run would
  mark — judged on that run's night, never on the day the preview runs, its
  hour pinned to cron.yaml's.

Through the real route, model and fake Firestore (``tests/_fake_firestore``);
« today » frozen at the one seam (``tests/_frozen_clock``); a fictitious year
and invented amounts.
"""

import logging
import os
import pathlib
import re
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest
from google.api_core import exceptions as gexc

_ATHENA = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ATHENA))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

with mock.patch("google.cloud.firestore.Client"):
    from models import full_reads
    from models import invoice as invoice_model
    import routes.taches_factures as tf
    import routes.tasks as tasks_routes
    import scripts.apercu_factures_echues as apercu

from flask import Flask  # noqa: E402

from config import Config  # noqa: E402
from tests._fake_firestore import install  # noqa: E402
from tests._frozen_clock import freeze_deadlines  # noqa: E402
from tests._real_app_probe import run_probe  # noqa: E402
from tz import MTL  # noqa: E402
from utils.format_fr import format_cents_fr  # noqa: E402

UTC = timezone.utc
CRON = {"X-Appengine-Cron": "true"}
URL = "/taches/factures/retards"
TODAY = date(2031, 3, 5)                  # a Wednesday, EST
NIGHT = datetime(2031, 3, 5, 0, 20, tzinfo=MTL).astimezone(UTC)   # the cron's hour


def _mtl(*parts: int) -> datetime:
    """A Montréal wall-clock instant, as the frozen clock takes it (UTC)."""
    return datetime(*parts, tzinfo=MTL).astimezone(UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    freeze_deadlines(monkeypatch, NIGHT)
    monkeypatch.setattr(Config, "FACTURES_RETARD_ACTIF", True)
    return f


@pytest.fixture
def client(fake):
    app = Flask(__name__)
    app.register_blueprint(tf.taches_factures_bp)
    return app.test_client()


def _seed(fake, iid: str, *, due: date | None, status: str = "envoyée",
          paid: int = 0, **over) -> None:
    doc = {
        "id": iid, "invoice_number": f"2031-F{iid[-3:]}", "status": status,
        "dossier_id": "dos-9", "dossier_file_number": "2031-009",
        "client_id": "cli-9", "client_name": "Cliente Exemple",
        "date": datetime(2031, 1, 6, tzinfo=UTC),
        "due_date": (datetime(due.year, due.month, due.day, tzinfo=UTC)
                     if due else None),
        "total": 57312, "retainer_applied": 0, "amount_due": 57312,
        "amount_paid": paid, "etag": f"etag-{iid}", "created_via": "web",
    }
    doc.update(over)
    fake.seed(f"invoices/{iid}", doc)


def _seed_night(fake) -> None:
    """Five sent invoices — three late on TODAY — and three that the sweep's
    read never sees."""
    _seed(fake, "inv-001", due=date(2031, 3, 1))     # Sat → Mon 3 → late Tue 4
    _seed(fake, "inv-002", due=date(2031, 3, 4))     # late since midnight
    _seed(fake, "inv-003", due=date(2031, 3, 5))     # due today: payable
    _seed(fake, "inv-004", due=date(2031, 2, 1))     # the longest late
    _seed(fake, "inv-005", due=date(2031, 2, 1), paid=57312)   # nothing owing
    _seed(fake, "inv-006", due=date(2031, 2, 1), status="en_retard")
    _seed(fake, "inv-007", due=date(2031, 2, 1), status="brouillon")
    _seed(fake, "inv-008", due=date(2031, 2, 1), status="payée", paid=57312)


def _status(fake, iid: str) -> str:
    return fake.peek(f"invoices/{iid}")["status"]


def _sweep_lines(caplog) -> list[dict]:
    return [r.json_fields for r in caplog.records
            if getattr(r, "json_fields", {}).get("event") == "invoice_overdue_sweep"]


def _run(client, caplog=None):
    if caplog is None:
        return client.get(URL, headers=CRON)
    with caplog.at_level(logging.INFO, logger="pallas.invoice"):
        return client.get(URL, headers=CRON)


# ══════════════════════════════════════════════════════════════════════
# 1. The guard and the switch
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("headers", [{}, {"X-Appengine-Cron": "false"},
                                     {"X-Appengine-Cron": "True"},
                                     {"X-AppEngine-QueueName": "portail"}],
                         ids=["none", "false", "capitalised", "a-queue"])
def test_403_without_the_cron_header_and_nothing_read(fake, client, headers):
    _seed_night(fake)
    fake.reset_logs()
    assert client.get(URL, headers=headers).status_code == 403
    assert fake.reads == [] and fake.commits == []


def test_the_switch_off_reads_and_writes_nothing(fake, client, monkeypatch, caplog):
    _seed_night(fake)
    monkeypatch.setattr(Config, "FACTURES_RETARD_ACTIF", False)
    fake.reset_logs()
    resp = _run(client, caplog)
    assert resp.status_code == 200 and resp.get_json() == {"actif": False}
    assert fake.reads == [] and fake.commits == []
    assert _sweep_lines(caplog) == []
    assert _status(fake, "inv-004") == "envoyée"


#: The switch's one line in app.yaml: the lowercase quoted value config.py
#: compares (``.lower() == "true"``), ON or OFF.
_SWITCH_LINE = re.compile(r'  FACTURES_RETARD_ACTIF: "(true|false)"')


def _switch_value(app_yaml: str) -> str:
    """The switch's value in *app_yaml* — once it is proved set by exactly
    one line outside the comments, in the form config.py reads."""
    lines = [line for line in app_yaml.splitlines()
             if "FACTURES_RETARD_ACTIF" in line
             and not line.lstrip().startswith("#")]
    assert len(lines) == 1, lines
    found = _SWITCH_LINE.fullmatch(lines[0])
    assert found, lines[0]
    return found.group(1)


def test_the_switch_follows_the_outlook_mirror_precedent():
    """config.py reads it exactly as MIROIR_OUTLOOK_ACTIF (the environment,
    default true); app.yaml sets it by one line, "true" or "false". WHICH one
    ships is a review fact (DEPLOYMENT.md §15, step 1), never a pin: a pin of
    "false" failed the runbook's one-line commit that turns the sweep on at
    Cloud Build's test step, before any deploy — and, re-pinned "true", it
    would have failed the one-line rollback (review of lot 2)."""
    config = (_ATHENA / "config.py").read_text(encoding="utf-8")
    for name in ("MIROIR_OUTLOOK_ACTIF", "FACTURES_RETARD_ACTIF"):
        assert (f'os.environ.get("{name}", "true").lower() == "true"'
                in config), name
    assert _switch_value((_ATHENA / "app.yaml").read_text(encoding="utf-8")) in (
        "true", "false")


@pytest.mark.parametrize("value", ["true", "false"], ids=["switch-on", "rollback"])
def test_the_runbooks_one_line_commit_passes_this_suite(value):
    """DEPLOYMENT.md §15 turns the sweep on — step 4 — and rolls it back by
    ONE line of app.yaml, pushed through Cloud Build, whose first step runs
    this whole suite before any deploy. That very edit, made on a copy of the
    real file, changes one line at most and passes the switch's check; the
    runbook names that very setting."""
    lines = (_ATHENA / "app.yaml").read_text(encoding="utf-8").splitlines()
    setting = f'FACTURES_RETARD_ACTIF: "{value}"'
    edited = [f"  {setting}" if _SWITCH_LINE.fullmatch(line) else line
              for line in lines]
    assert len(edited) == len(lines)
    assert sum(a != b for a, b in zip(lines, edited)) <= 1
    assert _switch_value("\n".join(edited)) == value
    runbook = " ".join((_ATHENA.parent / "DEPLOYMENT.md").read_text(
        encoding="utf-8").split())
    promise = {"true": f"a one-line commit sets `{setting}`",
               "false": 'a one-line commit sets it back to `"false"`'}[value]
    assert promise in runbook


# ══════════════════════════════════════════════════════════════════════
# 2. A failed read writes nothing, and says so
# ══════════════════════════════════════════════════════════════════════


def _fail_invoice_queries(monkeypatch, fake) -> list:
    """Every query on ``invoices`` fails at the transport — below every
    model reader, where a real failure happens."""
    server = fake._fake_server
    real = server.run_query
    failures = []

    def failing(request, metadata=None, **kwargs):
        query = request["structured_query"]._pb
        if any(f.collection_id == "invoices" for f in query.from_):
            failures.append(1)
            raise gexc.ServiceUnavailable("injected query failure")
        return real(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "run_query", failing)
    return failures


def test_a_failed_read_writes_nothing_and_says_so(fake, client, monkeypatch, caplog):
    _seed_night(fake)
    failures = _fail_invoice_queries(monkeypatch, fake)
    fake.reset_logs()
    resp = _run(client, caplog)
    assert failures
    assert resp.status_code == 200
    assert resp.get_json() == {"actif": True, "erreur": "lecture"}
    assert fake.commits == []
    (line,) = _sweep_lines(caplog)
    assert line == {"event": "invoice_overdue_sweep", "outcome": "failure",
                    "reason": "lecture", "error_type": "ServiceUnavailable"}
    (record,) = [r for r in caplog.records
                 if getattr(r, "json_fields", {}).get("event") == "invoice_overdue_sweep"]
    assert record.levelno == logging.ERROR


def test_a_truncated_read_writes_nothing(fake, client, monkeypatch, caplog):
    """Past the read's cap the rows are not the collection: never « these
    are all the sent invoices »."""
    _seed_night(fake)
    monkeypatch.setattr(full_reads, "FULL_READ_CAP", 2)
    fake.reset_logs()
    resp = _run(client, caplog)
    assert resp.get_json() == {"actif": True, "erreur": "lecture"}
    assert fake.commits == []
    (line,) = _sweep_lines(caplog)
    assert line["error_type"] == "TruncatedRead"


def test_the_read_is_one_strict_equality_on_the_sent_status(fake):
    """A single-field equality and no order: the automatic index serves it
    (firestore.indexes.json declares no override on invoices.status)."""
    _seed_night(fake)
    rows = invoice_model.list_sent_invoices_strict()
    assert sorted(r["id"] for r in rows) == [
        "inv-001", "inv-002", "inv-003", "inv-004", "inv-005"]
    import json
    indexes = json.loads((_ATHENA.parent / "firestore.indexes.json").read_text(
        encoding="utf-8"))
    assert not [o for o in indexes.get("fieldOverrides", [])
                if o.get("collectionGroup") == "invoices"
                and o.get("fieldPath") == "status"]


# ══════════════════════════════════════════════════════════════════════
# 3. What it marks, how, and what it says
# ══════════════════════════════════════════════════════════════════════


def test_it_marks_exactly_the_overdue_longest_late_first(fake, client, caplog):
    _seed_night(fake)
    fake.reset_logs()
    resp = _run(client, caplog)
    counts = {"seen": 5, "overdue": 3, "marked": 3, "refused": 0,
              "errors": 0, "remaining": 0}
    assert resp.status_code == 200
    assert resp.get_json() == {"actif": True, **counts}
    assert [_status(fake, f"inv-00{n}") for n in range(1, 9)] == [
        "en_retard", "en_retard", "envoyée", "en_retard", "envoyée",
        "en_retard", "brouillon", "payée"]
    marked = [path for c in fake.commits for _op, path in c.ops]
    assert marked == ["invoices/inv-004", "invoices/inv-001", "invoices/inv-002"]
    assert all(c.transaction is not None for c in fake.commits)
    (line,) = _sweep_lines(caplog)
    assert line == {"event": "invoice_overdue_sweep", "outcome": "success",
                    **counts}


def test_every_mark_is_stamped_cron_and_says_the_due_date_caused_it(
        fake, client, caplog):
    _seed_night(fake)
    _run(client, caplog)
    stored = fake.peek("invoices/inv-004")
    assert stored["updated_via"] == "cron" and stored["created_via"] == "web"
    assert stored["etag"] != "etag-inv-004"
    changes = [r.json_fields for r in caplog.records
               if getattr(r, "json_fields", {}).get("event") == "invoice_status_changed"]
    assert len(changes) == 3
    for line in changes:
        assert (line["from_status"], line["to_status"]) == ("envoyée", "en_retard")
        assert (line["cause"], line["via"]) == ("echeance", "cron")


def test_a_second_run_is_idempotent(fake, client, caplog):
    _seed_night(fake)
    _run(client)
    fake.reset_logs()
    resp = _run(client, caplog)
    assert resp.get_json() == {"actif": True, "seen": 2, "overdue": 0,
                               "marked": 0, "refused": 0, "errors": 0,
                               "remaining": 0}
    assert fake.commits == []
    (line,) = _sweep_lines(caplog)
    assert line["marked"] == 0


def test_the_cap_and_the_remaining_count(fake, client):
    """At most OVERDUE_SWEEP_CAP marks a run, the longest-late first; the rest
    is counted and marked the next night — never dropped."""
    assert invoice_model.OVERDUE_SWEEP_CAP == 100
    for n in range(103):
        # Due dates a day apart: inv-100..inv-102 are the most recently due
        # (a weekend shares its Monday's late day; the id breaks the tie).
        _seed(fake, f"inv-{n:03d}", due=date(2030, 11, 1) + timedelta(days=n))
    first = _run(client).get_json()
    assert (first["seen"], first["overdue"], first["marked"], first["remaining"]) == (
        103, 103, 100, 3)
    waiting = sorted(iid for iid, doc in fake.peek_collection("invoices").items()
                     if doc["status"] == "envoyée")
    assert waiting == ["inv-100", "inv-101", "inv-102"]
    second = _run(client).get_json()
    assert (second["seen"], second["marked"], second["remaining"]) == (3, 3, 0)
    fake.reset_logs()
    third = _run(client).get_json()
    assert (third["seen"], third["marked"]) == (0, 0) and fake.commits == []


def test_an_invoice_changed_since_the_read_is_refused_never_overwritten(
        fake, client, monkeypatch, caplog):
    """Paid, or merely edited, between the sweep's read and its mark: the
    etag it read no longer matches, so the model refuses — the payment
    stands, and the edited invoice (still late) waits for the next night's
    read rather than being written over a version the sweep never saw. Each
    is counted « refused », the others marked."""
    _seed_night(fake)
    real = invoice_model.list_sent_invoices_strict

    def read_then_change():
        rows = real()
        fake.external_write("invoices/inv-001", {
            **fake.peek("invoices/inv-001"), "status": "payée",
            "amount_paid": 57312, "etag": "etag-paid"})
        fake.external_write("invoices/inv-002", {
            **fake.peek("invoices/inv-002"), "notes": "Relance envoyée.",
            "etag": "etag-edited"})
        return rows

    monkeypatch.setattr(invoice_model, "list_sent_invoices_strict", read_then_change)
    body = _run(client, caplog).get_json()
    assert (body["marked"], body["refused"], body["errors"]) == (1, 2, 0)
    assert _status(fake, "inv-001") == "payée"
    assert fake.peek("invoices/inv-001")["etag"] == "etag-paid"
    assert _status(fake, "inv-002") == "envoyée"
    assert fake.peek("invoices/inv-002")["etag"] == "etag-edited"
    assert _status(fake, "inv-004") == "en_retard"
    refusals = [r.json_fields for r in caplog.records
                if getattr(r, "json_fields", {}).get("event") == "invoice_refused"]
    assert sorted((r["invoice_id"], r["reason"]) for r in refusals) == [
        ("inv-001", "stale_etag"), ("inv-002", "stale_etag")]
    # The next night reads the edited invoice afresh, and marks it.
    monkeypatch.setattr(invoice_model, "list_sent_invoices_strict", real)
    assert _run(client).get_json()["marked"] == 1
    assert _status(fake, "inv-002") == "en_retard"


def test_a_run_in_the_evening_band_marks_nothing_due_that_day(
        fake, client, monkeypatch):
    """Cloud Scheduler can run a job by hand, at any hour. At 21:30 in
    Montréal on 4 March (02:30 UTC on the 5th) the invoice due that day is
    still payable — the sweep judges on Montréal's day, never UTC's."""
    _seed(fake, "inv-031", due=date(2031, 3, 4))
    freeze_deadlines(monkeypatch,
                     datetime(2031, 3, 4, 21, 30, tzinfo=MTL).astimezone(UTC))
    fake.reset_logs()
    body = _run(client).get_json()
    assert (body["seen"], body["overdue"], body["marked"], body["refused"]) == (
        1, 0, 0, 0)
    assert fake.commits == [] and _status(fake, "inv-031") == "envoyée"


def test_errors_are_counted_and_the_run_goes_on(fake, client, monkeypatch, caplog):
    """An amount the rule cannot read, a commit the store refuses and a mark
    that raises each cost ONE error; every other invoice is still marked."""
    _seed_night(fake)
    _seed(fake, "inv-011", due=date(2031, 2, 3), amount_due="1 234,56")
    _seed(fake, "inv-012", due=date(2031, 2, 3))
    _seed(fake, "inv-013", due=date(2031, 2, 3))

    def refuse_inv_012(info):
        if any(path == "invoices/inv-012" for _op, path in info.ops):
            raise gexc.PermissionDenied("injected commit failure")

    remove = fake.add_commit_hook(refuse_inv_012)
    real = invoice_model.update_status_report

    def raise_on_inv_013(invoice_id, *a, **k):
        if invoice_id == "inv-013":
            raise RuntimeError("a defect")
        return real(invoice_id, *a, **k)

    monkeypatch.setattr(invoice_model, "update_status_report", raise_on_inv_013)
    try:
        body = _run(client, caplog).get_json()
    finally:
        remove()
    assert body == {"actif": True, "seen": 8, "overdue": 5, "marked": 3,
                    "refused": 0, "errors": 3, "remaining": 0}
    assert [_status(fake, i) for i in ("inv-011", "inv-012", "inv-013")] == [
        "envoyée"] * 3
    assert {_status(fake, i) for i in ("inv-001", "inv-002", "inv-004")} == {
        "en_retard"}
    lines = {r.getMessage(): r.json_fields for r in caplog.records
             if getattr(r, "json_fields", {}).get("event") == "unexpected"}
    assert sorted(lines) == ["invoice lateness unreadable",
                             "invoice overdue sweep: mark raised",
                             "invoice status update failed"]
    # The morning's « errors: 3 » leads to its three invoices: each line
    # names its own — one request marks up to a hundred (review of lot 2).
    assert {message: line["invoice_id"] for message, line in lines.items()} == {
        "invoice lateness unreadable": "inv-011",
        "invoice status update failed": "inv-012",
        "invoice overdue sweep: mark raised": "inv-013",
    }
    # The unreadable amount: its field, never its value — no line, no
    # traceback carries it (CLAUDE.md « Do not log PII »).
    assert lines["invoice lateness unreadable"]["field"] == "amount_due"
    assert not [r for r in caplog.records
                if "1 234,56" in logging.Formatter().format(r)]


# ══════════════════════════════════════════════════════════════════════
# 4. The route's place: /taches, the real app, cron.yaml
# ══════════════════════════════════════════════════════════════════════


def _adapter(*blueprints):
    app = Flask(__name__)
    for bp in blueprints:
        app.register_blueprint(bp)
    return app.url_map.bind("localhost")


@pytest.mark.parametrize("order", ["tasks-first", "sweep-first"])
def test_the_route_never_collides_with_the_task_list(order):
    """The web task list owns /taches/<task_id> and /taches/<task_id>/…;
    the sweep's two literal segments win whichever registers first, a task
    id still reaches the task list, and a POST to the sweep never reaches a
    task handler."""
    bps = [tasks_routes.tasks_bp, tf.taches_factures_bp]
    if order == "sweep-first":
        bps.reverse()
    adapter = _adapter(*bps)
    assert adapter.match(URL, method="GET")[0] == "taches_factures.retards"
    task_id = "0b5e6f3a-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
    assert adapter.match(f"/taches/{task_id}", method="GET")[0] == "tasks.task_detail"
    assert adapter.match(f"/taches/{task_id}/edit", method="GET")[0] == "tasks.task_edit"
    from werkzeug.exceptions import MethodNotAllowed
    with pytest.raises(MethodNotAllowed):
        adapter.match(URL, method="POST")


_REAL_APP_PROBE = textwrap.dedent('''
    import json

    import main
    from config import Config
    from security import csrf

    app = main.app
    exempt = {getattr(b, "name", b) for b in csrf._exempt_blueprints}
    bp = app.blueprints.get("taches_factures")
    endpoint, _args = app.url_map.bind("localhost").match(
        "/taches/factures/retards", method="GET")
    print("@@TACHES_FACTURES@@" + json.dumps({
        "registered": bp is not None,
        "import_name": bp.import_name if bp is not None else None,
        "exempt": "taches_factures" in exempt,
        "endpoint": endpoint,
        "rules": sorted(r.rule for r in app.url_map.iter_rules()
                        if r.endpoint.startswith("taches_factures.")),
        "switch_default": Config.FACTURES_RETARD_ACTIF,
    }))
''')


def test_the_real_app_registers_the_sweep_csrf_exempt():
    env = {**os.environ}
    env.pop("ENV", None)
    env.pop("FACTURES_RETARD_ACTIF", None)
    found = run_probe(_REAL_APP_PROBE, "@@TACHES_FACTURES@@", env=env, timeout=120)
    assert found == {
        "registered": True, "import_name": "routes.taches_factures",
        "exempt": True, "endpoint": "taches_factures.retards",
        "rules": ["/taches/factures/retards"],
        # Unset, the switch is ON (the precedent) — app.yaml turns it off.
        "switch_default": True,
    }


def _cron_jobs() -> list[dict]:
    """cron.yaml's entries — a flat list of « - key: value » blocks, read
    line by line (no YAML library in the lock)."""
    jobs: list[dict] = []
    for raw in (_ATHENA.parent / "cron.yaml").read_text(encoding="utf-8").splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#") or line == "cron:":
            continue
        if line.startswith("- "):
            jobs.append({})
            line = line[2:]
        key, _, value = line.strip().partition(":")
        jobs[-1][key.strip()] = value.strip()
    return jobs


def test_cron_yaml_schedules_the_sweep_at_0020_montreal():
    (job,) = [j for j in _cron_jobs() if j.get("url") == URL]
    assert job["schedule"] == "every day 00:20"
    # The preview counts its night from the same hour, restated there for
    # the computation (second review of lot 2): one source, pinned.
    assert job["schedule"] == f"every day {apercu.SWEEP_TIME:%H:%M}"
    assert job["timezone"] == "America/Montreal"
    assert job["target"] == "default"
    # Every other job keeps its entry: the file REPLACES the whole table.
    assert {j["url"] for j in _cron_jobs()} >= {
        "/taches/portail/reconciliation", "/taches/bookings/sync",
        "/taches/outlook/sync", URL}


def test_no_text_restates_the_job_count():
    """cron.yaml is the one list of the jobs (« One source per list »): the
    texts that said « three » no longer count them."""
    texts = {
        "cron.yaml": (_ATHENA.parent / "cron.yaml").read_text(encoding="utf-8"),
        "DEPLOYMENT.md": (_ATHENA.parent / "DEPLOYMENT.md").read_text(encoding="utf-8"),
        "deployment_inventory": (_ATHENA / "utils" / "deployment_inventory.py"
                                 ).read_text(encoding="utf-8"),
        "deployment_probe": (_ATHENA / "utils" / "deployment_probe.py"
                             ).read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        flat = " ".join(text.split()).lower()
        for stale in ("trois tâches cron", "three cron jobs",
                      "carrying three jobs", "compte trois", "all three cron"):
            assert stale not in flat, (name, stale)


# ══════════════════════════════════════════════════════════════════════
# 5. The preview — the same read, the same rule, the same order, on the
#    night of the next run
# ══════════════════════════════════════════════════════════════════════

#: The last line of every preview: what a switch turned on later changes.
_LATER_SWITCH = (
    "Ce balayage ne marque que si l'interrupteur FACTURES_RETARD_ACTIF est "
    "allumé d'ici là. Allumé une nuit plus tard, son premier balayage "
    "marquera aussi les factures devenues échues entre-temps : relancez cet "
    "aperçu le jour même de l'envoi de l'interrupteur.")


@pytest.mark.parametrize("preview_at", [
    _mtl(2031, 3, 4, 14, 0),     # the eve, in the daytime: the runbook's case
    _mtl(2031, 3, 5, 0, 10),     # the small hours, before that night's run
], ids=["eve-daytime", "before-0020"])
def test_the_preview_lists_what_the_next_night_marks_and_writes_nothing(
        fake, client, monkeypatch, capsys, preview_at):
    """REWRITTEN deliberately (second review of lot 2). The preview runs
    BEFORE the night it predicts — the runbook's order (DEPLOYMENT.md §15:
    the preview, the switch's push, then the next night) — and judges THAT
    night's day, never the day it runs: inv-002, due on the eve, is payable
    all that day and late by the 00:20 run, so the preview lists it, says it
    was payable until the eve, and the night marks it. The test used to run
    the preview at the sweep's own instant (NIGHT), which hid the day
    between them: run on the eve, the preview listed three invoices and the
    night marked four. From the small hours, the next run is that same
    night's — never the following one's, which would list inv-003, due that
    day."""
    _seed_night(fake)
    _seed(fake, "inv-021", due=date(2031, 2, 8), legacy_ref="facture:2030-118",
          imported=True, invoice_number="2030-118")
    freeze_deadlines(monkeypatch, preview_at)
    fake.reset_logs()
    assert apercu.main([]) == 0
    out = capsys.readouterr().out
    assert fake.commits == []
    lines = out.splitlines()
    table = [line for line in lines if line.count(" | ") == 5][1:]
    balance = format_cents_fr(57312)
    assert table == [
        f"2031-F004 | 2031-02-01 | 2031-02-03 | 30 | {balance} | non",
        f"2030-118 | 2031-02-08 | 2031-02-10 | 23 | {balance} | oui",
        f"2031-F001 | 2031-03-01 | 2031-03-03 | 2 | {balance} | non",
        f"2031-F002 | 2031-03-04 | 2031-03-04 | 1 | {balance} | non",
    ]
    assert lines[0] == (
        "Aperçu du prochain balayage des factures échues : la nuit du "
        "2031-03-05, à 00 h 20 (heure de Montréal). LECTURE SEULE : rien "
        "n'a été écrit.")
    assert "Factures envoyées lues : 6." in lines
    assert (f"4 facture(s) en retard cette nuit-là, "
            f"{format_cents_fr(4 * 57312)} de solde en tout.") in lines
    assert ("Dont 1 payable(s) jusqu'au 2031-03-04 inclusivement, en retard "
            "à compter du 2031-03-05 seulement.") in lines
    assert ("Le prochain balayage en marquerait 4 (au plus 100 par nuit, les "
            "plus anciennes d'abord).") in lines
    assert lines[-1] == _LATER_SWITCH
    # That night's run marks exactly those, in that order.
    freeze_deadlines(monkeypatch, NIGHT)
    fake.reset_logs()
    assert _run(client).get_json()["marked"] == 4
    marked = [path.split("/")[1] for c in fake.commits for _op, path in c.ops]
    assert marked == ["inv-004", "inv-021", "inv-001", "inv-002"]


def test_an_invoice_payable_on_the_previews_day_is_late_by_its_night(
        fake, client, monkeypatch, capsys):
    """The headline the second review of lot 2 caught: run on a Friday
    afternoon over one invoice due that Friday, the preview said « Aucune
    facture envoyée n'est en retard aujourd'hui : le premier balayage n'en
    marquerait aucune » — and the Saturday 00:20 run marked it. It judges
    that night now."""
    _seed(fake, "inv-031", due=date(2031, 3, 7))            # a Friday
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 7, 16, 0))
    assert apercu.main([]) == 0
    out = capsys.readouterr().out
    assert "aucune" not in out.lower()
    assert "la nuit du 2031-03-08, à 00 h 20" in out
    assert "Le prochain balayage en marquerait 1 " in out
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 8, 0, 20))
    assert _run(client).get_json()["marked"] == 1
    assert _status(fake, "inv-031") == "en_retard"


@pytest.mark.parametrize("local,night", [
    ((2031, 3, 4, 14, 0), date(2031, 3, 5)),     # daytime: that night's run
    ((2031, 3, 4, 23, 30), date(2031, 3, 5)),    # 04:30 UTC, already the 5th
    ((2031, 3, 5, 0, 19), date(2031, 3, 5)),     # that night's run is ahead
    ((2031, 3, 5, 0, 20), date(2031, 3, 6)),     # it is due: the next one
    ((2031, 7, 16, 0, 50), date(2031, 7, 17)),   # summer time (UTC−4)
], ids=["daytime", "evening", "0019", "0020", "edt"])
def test_the_next_sweep_night_is_read_on_montreals_clock(local, night):
    """The day the next 00:20 run judges, from Montréal's wall clock — never
    UTC's, whose day turns over at 19:00 (20:00 in summer) there: from the
    evening a UTC reading skips a night, from the small hours it skips that
    night's run."""
    assert apercu.next_sweep_night(_mtl(*local)) == night


_LATE_ROW = {"id": "i0", "number": "N0", "due": date(2031, 2, 3),
             "prorogued": date(2031, 2, 3), "days": 30, "balance_cents": 100,
             "imported": False}


@pytest.mark.parametrize("rows,unjudged", [
    ([], []), ([], ["bad-1"]), ([_LATE_ROW], []), ([_LATE_ROW], ["bad-1"]),
], ids=["none", "unjudged-only", "late", "late-and-unjudged"])
def test_every_preview_names_its_night_and_a_later_switch(rows, unjudged):
    """Whatever it finds, the preview names the night it judges and ends on
    what a switch turned on a later night changes — the runbook's « run it
    on the day of the push » (second review of lot 2)."""
    lines = apercu.report(rows, unjudged, night=TODAY, seen=3, cap=100)
    assert "la nuit du 2031-03-05, à 00 h 20 (heure de Montréal)" in lines[0]
    assert lines[-1] == _LATER_SWITCH


def test_the_preview_names_the_remainder_and_the_unjudged():
    rows = [{**_LATE_ROW, "id": f"i{n}", "number": f"N{n}"} for n in range(3)]
    lines = apercu.report(rows, ["bad-1"], night=TODAY, seen=4, cap=2)
    text = "\n".join(lines)
    assert ("Le prochain balayage en marquerait 2 (au plus 2 par nuit, les "
            "plus anciennes d'abord) ; 1 attendrait les nuits suivantes."
            ) in lines
    assert "bad-1" in text and "illisible" in text
    assert "aucune" not in text.lower()
    # None of them is late from that night only.
    assert not [line for line in lines if line.startswith("Dont ")]


def test_the_preview_with_nothing_late_says_so():
    lines = apercu.report([], [], night=TODAY, seen=2, cap=100)
    assert lines[3] == (
        "Aucune facture envoyée ne serait en retard cette nuit-là : le "
        "prochain balayage n'en marquerait aucune.")


def test_the_preview_never_says_none_is_late_over_the_unjudged():
    """Nothing judged late, but an invoice not judged: its lateness is
    unknown, not absent — the headline says so first, never « Aucune facture
    envoyée ne serait en retard » (review of lot 2: CLAUDE.md « A failed read
    never passes for absence »). The sweep would still mark none."""
    lines = apercu.report([], ["bad-1"], night=TODAY, seen=2, cap=100)
    text = "\n".join(lines)
    assert "Aucune facture envoyée" not in text
    assert lines[3] == (
        "Le retard de 1 facture(s) n'a pu être jugé (ci-dessous) ; aucune "
        "autre ne serait en retard cette nuit-là : le prochain balayage n'en "
        "marquerait aucune.")
    assert "bad-1" in text and "illisible" in text


def test_a_failed_read_raises_never_prints_none(fake, monkeypatch, capsys):
    _seed_night(fake)
    _fail_invoice_queries(monkeypatch, fake)
    with pytest.raises(gexc.ServiceUnavailable):
        apercu.main([])
    assert "aucune" not in capsys.readouterr().out.lower()


def test_the_preview_takes_no_argument_and_reads_nothing_then(fake, capsys):
    fake.reset_logs()
    assert apercu.main(["--apply"]) == 2
    assert fake.reads == [] and fake.commits == []


def test_the_preview_says_it_reads_production_and_writes_nothing():
    """The words, whatever the docstring's wrapping."""
    doc = " ".join(apercu.__doc__.split())
    assert "READS PRODUCTION" in doc and "Application Default Credentials" in doc
    assert "WRITES NOTHING" in doc
