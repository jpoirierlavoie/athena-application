"""The daily lateness sweep — ``routes/taches_factures`` (lot 2 of
« Factures : retard, radiation, intérêts », 2026-11-11), at 09:00 Montréal
time since the lawyer's decision of 2026-11-12.

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
- ``cron.yaml`` schedules it at 09:00, America/Toronto (Montréal's
  canonical tz zone, which agrees with the app's clock all year); the
  read-only preview (``scripts/apercu_factures_echues``) lists exactly what
  its NEXT run would mark — judged on that run's day, never on the day the
  preview runs, its hour pinned to cron.yaml's and read on Montréal's clock
  on both changes of time — and says what that list holds by: the switch
  serving before that run (a run due that very morning is too soon for a
  push after the preview, unless it serves by then), the preview run again
  otherwise — after a missed or failed first run too —, and after any
  invoice joins the list (repaired, or turned « envoyée »); past the cap the
  newest invoices wait, which every sentence allows for; the runbook and
  the observability row say the same (third review of lot 2, and the
  reviews of 9968616).

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
from zoneinfo import ZoneInfo

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
RUN = datetime(2031, 3, 5, 9, 0, tzinfo=MTL).astimezone(UTC)     # the cron's hour
#: cron.yaml's zone — Montréal's canonical name; tz.MTL reads its alias.
TORONTO = ZoneInfo("America/Toronto")


def _mtl(*parts: int) -> datetime:
    """A Montréal wall-clock instant, as the frozen clock takes it (UTC)."""
    return datetime(*parts, tzinfo=MTL).astimezone(UTC)


def _toronto(*parts: int, fold: int = 0) -> datetime:
    """A wall-clock instant in cron.yaml's zone, in UTC — *fold* picks the
    second pass of the hour lived twice when summer time ends."""
    return datetime(*parts, tzinfo=TORONTO, fold=fold).astimezone(UTC)


def _fake_modules() -> list:
    return [m for n, m in sorted(sys.modules.items())
            if (n.startswith("models.") or n == "dav.sync")
            and getattr(m, "db", None) is not None]


@pytest.fixture
def fake(monkeypatch):
    f = install(monkeypatch, *_fake_modules())
    freeze_deadlines(monkeypatch, RUN)
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


def _seed_run(fake) -> None:
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
    _seed_run(fake)
    fake.reset_logs()
    assert client.get(URL, headers=headers).status_code == 403
    assert fake.reads == [] and fake.commits == []


def test_the_switch_off_reads_and_writes_nothing(fake, client, monkeypatch, caplog):
    _seed_run(fake)
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
    _seed_run(fake)
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
    _seed_run(fake)
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
    _seed_run(fake)
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
    _seed_run(fake)
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
    _seed_run(fake)
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
    _seed_run(fake)
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
    is counted and marked by the next run — never dropped."""
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
    stands, and the edited invoice (still late) waits for the next run's
    read rather than being written over a version the sweep never saw. Each
    is counted « refused », the others marked."""
    _seed_run(fake)
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
    # The next run reads the edited invoice afresh, and marks it.
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
    _seed_run(fake)
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
    # The run's « errors: 3 » leads to its three invoices: each line
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


def test_cron_yaml_schedules_the_sweep_at_nine_montreal_time():
    """REWRITTEN deliberately (the lawyer's decision of 2026-11-12): the
    sweep runs every day at 09:00, America/Toronto — Montréal's canonical tz
    zone, America/Montreal being only its backward alias."""
    (job,) = [j for j in _cron_jobs() if j.get("url") == URL]
    assert job["schedule"] == "every day 09:00"
    # The preview counts its run from the same hour, restated there for
    # the computation (second review of lot 2): one source, pinned.
    assert job["schedule"] == f"every day {apercu.SWEEP_TIME:%H:%M}"
    assert job["timezone"] == "America/Toronto"
    assert job["timezone"] == TORONTO.key
    assert job["target"] == "default"
    # Every other job keeps its entry: the file REPLACES the whole table.
    assert {j["url"] for j in _cron_jobs()} >= {
        "/taches/portail/reconciliation", "/taches/bookings/sync",
        "/taches/outlook/sync", URL}


def test_cron_yamls_zone_keeps_the_apps_clock_all_year():
    """cron.yaml names America/Toronto; the app, the sweep and its preview
    read Montréal's clock through tz.MTL (America/Montreal). The preview's
    09:00 is the scheduler's only while the two names give every instant
    the same offset — both changes of time of 2031 included, hour by
    hour."""
    start = datetime(2031, 1, 1, tzinfo=UTC)
    for hours in range(366 * 24):
        instant = start + timedelta(hours=hours)
        assert (instant.astimezone(TORONTO).utcoffset()
                == instant.astimezone(MTL).utcoffset()), instant
    # The two changes of time, as the scheduler lives them: 09:00 is
    # 13:00 UTC from the second Sunday of March, 14:00 UTC from the first
    # Sunday of November.
    assert _toronto(2031, 3, 9, 9, 0) == datetime(2031, 3, 9, 13, 0, tzinfo=UTC)
    assert _toronto(2031, 3, 8, 9, 0) == datetime(2031, 3, 8, 14, 0, tzinfo=UTC)
    assert _toronto(2031, 11, 2, 9, 0) == datetime(2031, 11, 2, 14, 0, tzinfo=UTC)
    assert _toronto(2031, 11, 1, 9, 0) == datetime(2031, 11, 1, 13, 0, tzinfo=UTC)


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
#    day of the next run
# ══════════════════════════════════════════════════════════════════════

def _header(day: str) -> str:
    """The first line of every preview: the run it judges, named by its day
    and its hour — 9 h since the lawyer's decision of 2026-11-12."""
    return (
        f"Aperçu du prochain balayage des factures échues : le {day}, à 9 h "
        "(heure de Montréal). LECTURE SEULE : rien n'a été écrit.")


#: The second line of a preview run before 9 h: the run it names is that
#: very morning's — too soon for a switch pushed after the preview, unless
#: its version serves by then; else run the preview again after 9 h.
_TODAY_RUN = (
    "Ce balayage est celui d'aujourd'hui, à 9 h : trop tôt pour un "
    "interrupteur envoyé après cet aperçu, à moins que sa version ne serve "
    "avant cette heure. Sinon, relancez cet aperçu après 9 h : il nommera le "
    "balayage du lendemain.")


def _closing(day: str) -> list[str]:
    """The last two lines of every preview: the deadline its list holds by —
    the switch SERVING before that run, never « le jour même de l'envoi » —
    then the invoices that may join the list before then (third review of
    lot 2). The invoices fallen due before a later first run are marked
    « ce jour-là ou les suivants »: past the cap, the newest wait (review of
    9968616). Restated in full: a reworded text must fail here."""
    return [
        "Ce balayage ne marque cette liste que si l'interrupteur "
        "FACTURES_RETARD_ACTIF est allumé en production — son commit envoyé, "
        f"sa version servie — avant le {day} à 9 h. Sinon, son premier "
        "balayage sera celui d'un jour suivant, et les factures devenues "
        "échues entre-temps, que cette liste ne montre pas, seront marquées "
        "elles aussi, ce jour-là ou les suivants : relancez alors cet aperçu "
        "après ce 9 h, et revoyez sa liste avant le balayage qu'il nommera.",
        "Relancez-le aussi après toute facture passée à « envoyée » d'ici là "
        "(un brouillon envoyé, une facture rouverte ou dont le retard est "
        f"retiré) : en retard le {day}, elle serait marquée sans avoir paru "
        "ici.",
    ]


def _unjudged_line(ids: str) -> str:
    """The line of the invoices the preview cannot judge: repair them, then
    run it again (third review of lot 2) — « une fois réparée, une facture
    en retard », never « une facture réparée en retard », which reads first
    as « repaired late » (review of 9968616). Restated in full."""
    count = len(ids.split(", "))
    return (
        f"{count} facture(s) dont le retard ne peut être jugé (un montant "
        "stocké illisible) — le balayage les compterait en erreur et n'y "
        f"toucherait pas : {ids}. Réparez-les, puis relancez cet aperçu : une "
        "fois réparée, une facture en retard s'ajoute à la liste de l'aperçu.")


@pytest.mark.parametrize("preview_at,runs_today", [
    (_mtl(2031, 3, 4, 14, 0), False),   # the eve, after 9 h: the runbook's case
    (_mtl(2031, 3, 4, 9, 1), False),    # the eve, the minute after its run
    (_mtl(2031, 3, 5, 0, 0), True),     # midnight: that morning's run is ahead
    (_mtl(2031, 3, 5, 8, 59), True),    # the minute before it
], ids=["eve-afternoon", "eve-0901", "midnight", "0859"])
def test_the_preview_lists_what_the_next_run_marks_and_writes_nothing(
        fake, client, monkeypatch, capsys, preview_at, runs_today):
    """REWRITTEN deliberately (second review of lot 2; its texts by the
    third; its hour by the lawyer's decision of 2026-11-12). The preview
    runs BEFORE the run it predicts — the runbook's order (DEPLOYMENT.md
    §15: the preview, the switch's push, then the next 9 h) — and judges
    THAT run's day, never the day it runs: inv-002, due on the eve, is
    payable all that day and late by the 9 h run, so the preview lists it,
    says it was payable until the eve, and the run marks it. Run on the eve
    after 9 h, the preview names the next day's run; from midnight to 8:59,
    that same morning's — never the following day's, which would list
    inv-003, due that day — and it says, second, that this run is today's:
    too soon for a switch pushed after it, unless it serves by then."""
    _seed_run(fake)
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
    assert lines[0] == _header("2031-03-05")
    assert lines.count(_TODAY_RUN) == int(runs_today)
    assert lines[1] == (_TODAY_RUN if runs_today else "Factures envoyées lues : 6.")
    assert "Factures envoyées lues : 6." in lines
    assert (f"4 facture(s) en retard ce jour-là, "
            f"{format_cents_fr(4 * 57312)} de solde en tout.") in lines
    assert ("Dont 1 payable(s) jusqu'au 2031-03-04 inclusivement, en retard "
            "à compter du 2031-03-05 seulement.") in lines
    assert ("Le prochain balayage en marquerait 4 (au plus 100 par balayage, "
            "les plus anciennes d'abord).") in lines
    assert lines[-2:] == _closing("2031-03-05")
    # That run marks exactly those, in that order.
    freeze_deadlines(monkeypatch, RUN)
    fake.reset_logs()
    assert _run(client).get_json()["marked"] == 4
    marked = [path.split("/")[1] for c in fake.commits for _op, path in c.ops]
    assert marked == ["inv-004", "inv-021", "inv-001", "inv-002"]


def test_an_invoice_payable_on_the_previews_day_is_late_by_its_run(
        fake, client, monkeypatch, capsys):
    """The headline the second review of lot 2 caught: run on a Friday
    afternoon over one invoice due that Friday, the preview said « Aucune
    facture envoyée n'est en retard aujourd'hui : le premier balayage n'en
    marquerait aucune » — and the next run marked it. It judges that run's
    day: the Saturday's, at 9 h."""
    _seed(fake, "inv-031", due=date(2031, 3, 7))            # a Friday
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 7, 16, 0))
    assert apercu.main([]) == 0
    out = capsys.readouterr().out
    assert "aucune" not in out.lower()
    assert out.splitlines()[0] == _header("2031-03-08")
    assert "Le prochain balayage en marquerait 1 " in out
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 8, 9, 0))
    assert _run(client).get_json()["marked"] == 1
    assert _status(fake, "inv-031") == "en_retard"


@pytest.mark.parametrize("now,day", [
    (_mtl(2031, 3, 4, 14, 0), date(2031, 3, 5)),      # the afternoon: tomorrow's run
    (_mtl(2031, 3, 4, 19, 30), date(2031, 3, 5)),     # 00:30 UTC: UTC's day has turned
    (_mtl(2031, 3, 4, 23, 59), date(2031, 3, 5)),
    (_mtl(2031, 3, 5, 0, 0), date(2031, 3, 5)),       # midnight: this morning's run
    (_mtl(2031, 3, 5, 8, 59), date(2031, 3, 5)),      # 13:59 UTC, past UTC's 09:00
    (_mtl(2031, 3, 5, 9, 0), date(2031, 3, 6)),       # the run is due: the next one
    (_mtl(2031, 3, 5, 9, 1), date(2031, 3, 6)),
    (_mtl(2031, 7, 16, 8, 59), date(2031, 7, 16)),    # summer time (UTC−4)
    (_mtl(2031, 7, 16, 9, 0), date(2031, 7, 17)),
    (_toronto(2031, 3, 9, 8, 59), date(2031, 3, 9)),  # the day summer time begins
    (_toronto(2031, 3, 9, 9, 0), date(2031, 3, 10)),
    (_toronto(2031, 3, 9, 9, 30), date(2031, 3, 10)),     # 08:30 on a winter clock
    (_toronto(2031, 11, 2, 1, 30, fold=1), date(2031, 11, 2)),  # the hour lived twice
    (_toronto(2031, 11, 2, 8, 30), date(2031, 11, 2)),    # 09:30 on a summer clock
    (_toronto(2031, 11, 2, 9, 0), date(2031, 11, 3)),     # the day summer time ends
], ids=["afternoon", "evening-utc-turned", "2359", "midnight", "0859", "0900",
        "0901", "edt-0859", "edt-0900", "spring-0859", "spring-0900",
        "spring-0930", "fall-0130-twice", "fall-0830", "fall-0900"])
def test_the_next_sweep_day_is_read_on_montreals_clock(now, day):
    """The day the next 9 h run judges, from Montréal's wall clock — never
    UTC's, whose day turns over at 19:00 (20:00 in summer) there and whose
    09:00 is Montréal's 04:00 (05:00 in summer): from then to 8:59 a UTC
    reading skips that morning's run. Nor a frozen offset: on each change of
    time, one of them misreads the hour around 9 h."""
    assert apercu.next_sweep_day(now) == day


@pytest.mark.parametrize("now,today", [
    (_mtl(2031, 3, 4, 23, 59), False),    # the evening: tomorrow's run
    (_mtl(2031, 3, 4, 19, 10), False),    # 00:10 UTC — UTC's small hours, Montréal's evening
    (_mtl(2031, 3, 5, 0, 0), True),       # midnight: this morning's run is ahead
    (_mtl(2031, 3, 5, 8, 59), True),
    (_mtl(2031, 3, 5, 8, 59, 59, 999999), True),
    (_mtl(2031, 3, 5, 9, 0), False),      # the run is due: the next is tomorrow's
    (_mtl(2031, 3, 5, 9, 1), False),
    (_mtl(2031, 7, 16, 8, 59), True),     # summer time (UTC−4)
    (_mtl(2031, 7, 16, 9, 0), False),
    (_toronto(2031, 3, 9, 8, 59), True),  # the day summer time begins
    (_toronto(2031, 3, 9, 9, 0), False),
    (_toronto(2031, 3, 9, 9, 30), False),
    (_toronto(2031, 11, 2, 1, 30, fold=1), True),   # the hour lived twice
    (_toronto(2031, 11, 2, 8, 30), True),           # the day summer time ends
    (_toronto(2031, 11, 2, 9, 0), False),
], ids=["2359", "utc-small-hours", "midnight", "0859", "0859-59", "0900",
        "0901", "edt-0859", "edt-0900", "spring-0859", "spring-0900",
        "spring-0930", "fall-0130-twice", "fall-0830", "fall-0900"])
def test_the_next_run_is_today_only_before_nine(now, today):
    """THIRD review of lot 2, at 9 h since 2026-11-12. A preview run before
    9 h names that very morning's run — too soon for a switch pushed after
    it, unless its version serves by then. next_run_is_today says so, on
    Montréal's clock — never UTC's, whose small hours are Montréal's evening
    and whose 09:00 comes at Montréal's 04:00 — exactly when next_sweep_day
    names the preview's own day; 9 h itself names the next day's."""
    assert apercu.next_run_is_today(now) is today
    assert today == (apercu.next_sweep_day(now) == now.astimezone(MTL).date())


@pytest.mark.parametrize("preview_at,day,today,days,run_at", [
    (_toronto(2031, 3, 9, 8, 59), "2031-03-09", True, 2,
     _toronto(2031, 3, 9, 9, 0)),
    (_toronto(2031, 3, 9, 9, 0), "2031-03-10", False, 3,
     _toronto(2031, 3, 10, 9, 0)),
    (_toronto(2031, 11, 2, 8, 30), "2031-11-02", True, 2,
     _toronto(2031, 11, 2, 9, 0)),
    (_toronto(2031, 11, 2, 9, 30), "2031-11-03", False, 3,
     _toronto(2031, 11, 3, 9, 0)),
], ids=["spring-0859", "spring-0900", "fall-0830", "fall-0930"])
def test_on_both_changes_of_time_the_preview_names_the_schedulers_run(
        fake, client, monkeypatch, capsys, preview_at, day, today, days, run_at):
    """The two Sundays of 2031 when the clocks change. Frozen at an instant
    of cron.yaml's zone, the preview names the run Cloud Scheduler fires at
    9 h that day or the next, says whether it is today's, judges its day —
    the invoice due on the Friday before, late from the Saturday, is late
    by 2 or 3 days — and that run marks exactly its list."""
    _seed(fake, "inv-401", due=date(2031, 3, 7))       # the Friday before spring
    _seed(fake, "inv-402", due=date(2031, 10, 31))     # the Friday before fall
    fresh = "2031-F401" if day.startswith("2031-03") else "2031-F402"
    freeze_deadlines(monkeypatch, preview_at)
    assert apercu.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == _header(day)
    assert lines.count(_TODAY_RUN) == int(today)
    table = [line for line in lines if line.count(" | ") == 5][1:]
    (row,) = [line for line in table if line.startswith(fresh + " ")]
    assert row.split(" | ")[3] == str(days)
    assert lines[-2:] == _closing(day)
    freeze_deadlines(monkeypatch, run_at)
    fake.reset_logs()
    assert _run(client).get_json()["marked"] == len(table)


_LATE_ROW = {"id": "i0", "number": "N0", "due": date(2031, 2, 3),
             "prorogued": date(2031, 2, 3), "days": 30, "balance_cents": 100,
             "imported": False}
_FRESH_ROW = {**_LATE_ROW, "id": "i100", "number": "N100",
              "due": date(2031, 3, 4), "prorogued": date(2031, 3, 4), "days": 1}
_SHAPES = {
    "none": ([], []),
    "unjudged-only": ([], ["bad-1"]),
    "late": ([_LATE_ROW], []),
    "late-and-unjudged": ([_LATE_ROW], ["bad-1"]),
    # Past the cap of 100: the newest, late from the run's day only, waits.
    "over-the-cap": ([{**_LATE_ROW, "id": f"i{n}", "number": f"N{n}"}
                      for n in range(100)] + [_FRESH_ROW], []),
}


def _body(shape: str) -> list[str]:
    """Every line a shape prints between « Factures envoyées lues » and the
    closing lines — restated in full."""
    one = format_cents_fr(100)
    head = "Facture | échéance | reportée au | jours de retard | solde | importée"
    late = [head, f"N0 | 2031-02-03 | 2031-02-03 | 30 | {one} | non", "",
            f"1 facture(s) en retard ce jour-là, {one} de solde en tout.",
            "Le prochain balayage en marquerait 1 (au plus 100 par balayage, "
            "les plus anciennes d'abord)."]
    return {
        "none": ["Aucune facture envoyée ne serait en retard ce jour-là : le "
                 "prochain balayage n'en marquerait aucune."],
        "unjudged-only": [
            "Le retard de 1 facture(s) n'a pu être jugé (ci-dessous) ; aucune "
            "autre ne serait en retard ce jour-là : le prochain balayage n'en "
            "marquerait aucune.", "", _unjudged_line("bad-1")],
        "late": late,
        "late-and-unjudged": [*late, "", _unjudged_line("bad-1")],
        "over-the-cap": [
            head,
            *[f"N{n} | 2031-02-03 | 2031-02-03 | 30 | {one} | non"
              for n in range(100)],
            f"N100 | 2031-03-04 | 2031-03-04 | 1 | {one} | non",
            "",
            f"101 facture(s) en retard ce jour-là, {format_cents_fr(10100)} "
            "de solde en tout.",
            "Dont 1 payable(s) jusqu'au 2031-03-04 inclusivement, en retard à "
            "compter du 2031-03-05 seulement.",
            "Le prochain balayage en marquerait 100 (au plus 100 par balayage, "
            "les plus anciennes d'abord) ; 1 attendrait les balayages "
            "suivants."],
    }[shape]


@pytest.mark.parametrize("runs_today", [False, True], ids=["after-9h", "before-9h"])
@pytest.mark.parametrize("shape", list(_SHAPES))
def test_every_preview_names_its_run_and_what_its_list_holds_by(
        shape, runs_today):
    """REWRITTEN deliberately (third review of lot 2; the hour of
    2026-11-12; the cap of the review of 9968616). Whatever it finds —
    nothing, only invoices it cannot judge, late ones, both, more than the
    cap of 100 — the preview names the run it judges by its day and hour,
    says second whether that run is today's, and ends on the deadline its
    list holds by — the switch serving before that run, else the preview
    run again after it — then on the invoices that may join the list before
    then. Every line of every shape is restated: none may say what is false
    in another."""
    rows, unjudged = _SHAPES[shape]
    lines = apercu.report(rows, unjudged, day=TODAY, runs_today=runs_today,
                          seen=3, cap=100)
    assert lines == [
        _header("2031-03-05"),
        *([_TODAY_RUN] if runs_today else []),
        "Factures envoyées lues : 3.", "",
        *_body(shape),
        "", *_closing("2031-03-05"),
    ]


def test_the_preview_names_the_remainder_and_the_unjudged():
    """The unjudged line says to repair, then run the preview again: once
    repaired, a late invoice joins its list (third review of lot 2 —
    repaired as step 3 said, it was marked without ever being listed). The
    rest past the cap waits for the following runs, one or several."""
    rows = [{**_LATE_ROW, "id": f"i{n}", "number": f"N{n}"} for n in range(4)]
    lines = apercu.report(rows[:3], ["bad-1"], day=TODAY, runs_today=False,
                          seen=4, cap=2)
    text = "\n".join(lines)
    assert ("Le prochain balayage en marquerait 2 (au plus 2 par balayage, les "
            "plus anciennes d'abord) ; 1 attendrait les balayages suivants."
            ) in lines
    assert _unjudged_line("bad-1") in lines
    assert "aucune" not in text.lower()
    # None of them is late from that day only.
    assert not [line for line in lines if line.startswith("Dont ")]
    lines = apercu.report(rows, [], day=TODAY, runs_today=False, seen=4, cap=2)
    assert ("Le prochain balayage en marquerait 2 (au plus 2 par balayage, les "
            "plus anciennes d'abord) ; 2 attendraient les balayages suivants."
            ) in lines


def test_the_preview_with_nothing_late_says_so():
    lines = apercu.report([], [], day=TODAY, runs_today=False, seen=2, cap=100)
    assert lines[3] == (
        "Aucune facture envoyée ne serait en retard ce jour-là : le "
        "prochain balayage n'en marquerait aucune.")


def test_the_preview_never_says_none_is_late_over_the_unjudged():
    """Nothing judged late, but an invoice not judged: its lateness is
    unknown, not absent — the headline says so first, never « Aucune facture
    envoyée ne serait en retard » (review of lot 2: CLAUDE.md « A failed read
    never passes for absence »). The sweep would still mark none."""
    lines = apercu.report([], ["bad-1"], day=TODAY, runs_today=False, seen=2,
                          cap=100)
    text = "\n".join(lines)
    assert "Aucune facture envoyée" not in text
    assert lines[3] == (
        "Le retard de 1 facture(s) n'a pu être jugé (ci-dessous) ; aucune "
        "autre ne serait en retard ce jour-là : le prochain balayage n'en "
        "marquerait aucune.")
    assert "bad-1" in text and "illisible" in text


def test_a_failed_read_raises_never_prints_none(fake, monkeypatch, capsys):
    _seed_run(fake)
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


# ══════════════════════════════════════════════════════════════════════
# 6. The switch's first run is the one the preview's LAST run named
#    (third review of lot 2; the reviews of 9968616)
# ══════════════════════════════════════════════════════════════════════


def test_a_preview_before_nine_says_so_and_its_rerun_lists_the_first_run(
        fake, client, monkeypatch, capsys, caplog):
    """THIRD review of lot 2, at 9 h since 2026-11-12. Run at 08:50, the
    preview names that same morning's run, ten minutes away; a switch
    pushed after it, at 10:00, misses that run — which finds it off and
    logs nothing — and its first run, the next day's, also marks inv-102,
    due on the named day and never listed. The preview says, second, that
    its run is today's and to run it again after 9 h; run again, it names
    the next day's run and lists inv-102, and that run marks exactly its
    list."""
    _seed(fake, "inv-101", due=date(2031, 3, 3))       # Monday: late from Tuesday
    _seed(fake, "inv-102", due=date(2031, 3, 5))       # Wednesday: late from Thursday
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 5, 8, 50))
    assert apercu.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == [_header("2031-03-05"), _TODAY_RUN]
    assert ("Le prochain balayage en marquerait 1 (au plus 100 par balayage, "
            "les plus anciennes d'abord).") in lines
    # Ten minutes later the switch is still off: that run logs nothing, so
    # step 5 of the runbook finds NO line after 9 h.
    monkeypatch.setattr(Config, "FACTURES_RETARD_ACTIF", False)
    freeze_deadlines(monkeypatch, RUN)
    assert _run(client, caplog).get_json() == {"actif": False}
    assert _sweep_lines(caplog) == []
    # Run again after 9 h, as it says: the next day's run, inv-102 listed.
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 5, 9, 55))
    assert apercu.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == _header("2031-03-06")
    assert _TODAY_RUN not in lines
    assert ("Le prochain balayage en marquerait 2 (au plus 100 par balayage, "
            "les plus anciennes d'abord).") in lines
    assert lines[-2:] == _closing("2031-03-06")
    # Pushed at 10:00 and serving by the next 9 h: its first run marks that
    # list.
    monkeypatch.setattr(Config, "FACTURES_RETARD_ACTIF", True)
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 6, 9, 0))
    fake.reset_logs()
    assert _run(client).get_json()["marked"] == 2
    assert [_status(fake, i) for i in ("inv-101", "inv-102")] == [
        "en_retard", "en_retard"]


def test_a_failed_first_run_reads_like_no_line_and_the_rerun_lists_the_next(
        fake, client, monkeypatch, capsys, caplog):
    """Review of 9968616 (A). The switch serves, but its first run's strict
    read fails: {"actif": true, "erreur": "lecture"}, nothing written. Like
    a run that found the switch off, it leaves the first marks to the next
    run — which marks 2, inv-302 among them, fallen due in between and on
    no list. Step 5 read « wrote nothing; the next night retries »; it now
    has the preview run again before that run: it lists inv-302, and the
    run marks exactly that list."""
    _seed(fake, "inv-301", due=date(2031, 3, 3))       # Monday: late from Tuesday
    _seed(fake, "inv-302", due=date(2031, 3, 5))       # Wednesday: late from Thursday
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 4, 14, 0))
    assert apercu.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == _header("2031-03-05")
    table = [line for line in lines if line.count(" | ") == 5][1:]
    assert [row.split(" | ")[0] for row in table] == ["2031-F301"]
    # The switch serves; the 9 h run's read fails, and writes nothing.
    server = fake._fake_server
    working = server.run_query
    failures = _fail_invoice_queries(monkeypatch, fake)
    freeze_deadlines(monkeypatch, RUN)
    fake.reset_logs()
    assert _run(client, caplog).get_json() == {"actif": True, "erreur": "lecture"}
    assert failures and fake.commits == []
    assert [line["outcome"] for line in _sweep_lines(caplog)] == ["failure"]
    monkeypatch.setattr(server, "run_query", working)
    # Run again before the next run, as step 5 now says: inv-302 listed.
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 5, 10, 0))
    assert apercu.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == _header("2031-03-06")
    table = [line for line in lines if line.count(" | ") == 5][1:]
    assert [row.split(" | ")[0] for row in table] == ["2031-F301", "2031-F302"]
    # The next run marks exactly that list: two, where the first preview
    # listed one.
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 6, 9, 0))
    fake.reset_logs()
    body = _run(client).get_json()
    assert (body["marked"], body["remaining"]) == (2, 0)
    assert [_status(fake, i) for i in ("inv-301", "inv-302")] == [
        "en_retard", "en_retard"]


def test_past_the_cap_a_later_first_run_leaves_the_newest_to_the_next(
        fake, client, monkeypatch, capsys):
    """Review of 9968616 (B). « A later first run also marks the invoices
    fallen due in between » is false past the cap: a run marks the
    longest-late first, so the newest wait in `remaining`. The cap lowered
    to 2, the preview names the 5th's run and lists three — two to mark,
    one waiting; the switch misses that run; the 6th's, its first, marks
    the two longest-late and leaves inv-503 and inv-504 — fallen due in
    between — to the 7th's. The closing line says « ce jour-là ou les
    suivants »: true here too."""
    monkeypatch.setattr(invoice_model, "OVERDUE_SWEEP_CAP", 2)
    _seed(fake, "inv-501", due=date(2031, 2, 3))
    _seed(fake, "inv-502", due=date(2031, 2, 10))
    _seed(fake, "inv-503", due=date(2031, 3, 3))       # Monday: late from Tuesday
    _seed(fake, "inv-504", due=date(2031, 3, 5))       # Wednesday: late from Thursday
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 4, 14, 0))
    assert apercu.main([]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert ("Le prochain balayage en marquerait 2 (au plus 2 par balayage, "
            "les plus anciennes d'abord) ; 1 attendrait les balayages "
            "suivants.") in lines
    assert lines[-2:] == _closing("2031-03-05")
    # The switch is still off at the 5th's 9 h; it serves by the 6th's.
    monkeypatch.setattr(Config, "FACTURES_RETARD_ACTIF", False)
    freeze_deadlines(monkeypatch, RUN)
    assert _run(client).get_json() == {"actif": False}
    monkeypatch.setattr(Config, "FACTURES_RETARD_ACTIF", True)
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 6, 9, 0))
    fake.reset_logs()
    first = _run(client).get_json()
    assert (first["overdue"], first["marked"], first["remaining"]) == (4, 2, 2)
    marked = [path.split("/")[1] for c in fake.commits for _op, path in c.ops]
    assert marked == ["inv-501", "inv-502"]
    assert _status(fake, "inv-504") == "envoyée"       # fallen due in between: it waits
    # The following run marks it: « ce jour-là ou les suivants ».
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 7, 9, 0))
    assert _run(client).get_json()["marked"] == 2
    assert [_status(fake, i) for i in ("inv-503", "inv-504")] == [
        "en_retard", "en_retard"]


def test_a_repaired_invoice_joins_the_rerun_and_the_run_marks_the_last_list(
        fake, client, monkeypatch, capsys):
    """THIRD review of lot 2. Step 3 of the runbook had the lawyer repair the
    invoice the preview could not judge, and asked no re-run: repaired that
    afternoon, inv-119 — late since January — was marked by the next run
    without ever being listed, and step 5 expected the preview's 2, not 3.
    The unjudged line says to repair, then run the preview again; run again,
    it lists inv-119, and the run marks exactly its last list."""
    _seed(fake, "inv-111", due=date(2031, 2, 3))
    _seed(fake, "inv-112", due=date(2031, 3, 3))
    _seed(fake, "inv-119", due=date(2031, 1, 6), amount_due="1 234,56")
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 4, 14, 0))
    assert apercu.main([]) == 0
    out = capsys.readouterr().out
    assert "Le prochain balayage en marquerait 2 " in out
    assert _unjudged_line("inv-119") in out.splitlines()
    fake.external_write("invoices/inv-119", {
        **fake.peek("invoices/inv-119"), "amount_due": 57312})
    freeze_deadlines(monkeypatch, _mtl(2031, 3, 4, 16, 0))
    assert apercu.main([]) == 0
    out = capsys.readouterr().out
    assert "Le prochain balayage en marquerait 3 " in out
    assert f"2031-F119 | 2031-01-06 | 2031-01-06 | 58 | {format_cents_fr(57312)}" in out
    assert "Réparez-les" not in out
    freeze_deadlines(monkeypatch, RUN)
    fake.reset_logs()
    assert _run(client).get_json()["marked"] == 3
    assert _status(fake, "inv-119") == "en_retard"


def _lot2_steps() -> tuple[str, str, str]:
    """Steps 3, 4 and 5 of DEPLOYMENT.md §15's lot 2 entry, whitespace
    flattened."""
    text = " ".join((_ATHENA.parent / "DEPLOYMENT.md").read_text(
        encoding="utf-8").split())
    entry = text[text.index(
        "**Invoices: lateness follows the due date (2026-11-11).**"):]
    entry = entry[:entry.index("## 16. Troubleshooting")]
    step3 = entry.index("3. *The preview*")
    step4 = entry.index("4. *The switch on*")
    step5 = entry.index("5. *After 09:00 on the day the preview named*")
    end = entry.index("A rollback is the switch")
    assert step3 < step4 < step5 < end
    return entry[step3:step4], entry[step4:step5], entry[step5:end]


def test_the_runbook_keys_the_preview_on_the_named_run():
    """THIRD review of lot 2, at 9 h since 2026-11-12. Step 3 ran the preview
    « ON THE DAY the switch commit is pushed » — a calendar day, which a
    preview run before the sweep's hour (its run that very morning) met
    while the switch missed the run the preview named; step 5 then found no
    line, and said nothing of it. The steps key on that run: the preview
    after 09:00; the push any hour of that day, the version serving before
    the 09:00 it named — else the preview run again —; step 5 read after
    that run, saying what NO line means."""
    step3, step4, step5 = _lot2_steps()
    hour = f"{apercu.SWEEP_TIME:%H:%M}"
    assert "ON THE DAY the switch commit is pushed" not in step3
    assert (f"AFTER {hour} on the day the switch commit is pushed — never "
            f"before {hour}, when the run it names is that very morning's"
            ) in step3
    assert f"« le AAAA-MM-JJ, à {apercu._hour()} (heure de Montréal) »" in step3
    assert ("Any hour of that day works, provided the version SERVES before "
            f"the {hour} the preview named, the next day's") in step4
    assert "`gcloud app versions list --service=default`" in step4
    assert f"run the preview again after that {hour}" in step4
    assert "NO line at all: the switch was not serving yet" in step5
    assert "logs nothing (step 2)" in step5
    assert "read this line after the run it names" in step5
    for step in (step3, step4, step5):
        assert "night" not in step and "00:20" not in step


def test_the_runbook_runs_the_preview_again_once_an_invoice_joins_its_list():
    """THIRD review of lot 2, and the review of 9968616 (C, D). Step 3 has
    the lawyer repair the invoices the preview could not judge, then run it
    again — once repaired, a late invoice joins its list —, and again after
    any invoice turns « envoyée »; step 5 counts from its LAST run, give or
    take what changed since — less by those paid or voided, or marked « en
    retard » by hand (no longer « envoyée », the sweep never reads them)."""
    step3, _step4, step5 = _lot2_steps()
    assert "so it is repaired first, then the preview run again" in step3
    assert "once repaired, an invoice that is late joins the preview's list" in step3
    assert "the preview is run again after any such change" in step3
    assert "Its LAST run before that 09:00 is the list step 5 checks" in step3
    assert ("`marked` the count of the preview's last run before that 09:00"
            in step5)
    assert ("less by those paid or voided, or marked « en retard » by hand "
            "(the sheet's « Marquer en retard » or the connector's "
            "`update_invoice`: no longer « envoyée », the sweep never reads "
            "them)") in step5
    assert "more by any sent, reopened, unmarked or repaired" in step5


def test_the_runbook_reads_a_failed_first_run_like_no_line():
    """Review of 9968616 (A). Step 5 said a `failure` « wrote nothing; the
    next night retries »: true, but the retry is then the switch's first
    marking run, and it also marks the invoices fallen due in between, on
    no list. It now reads like no line: run the preview again before that
    run, then read the line after the run it names."""
    _step3, _step4, step5 = _lot2_steps()
    assert "retries" not in step5
    assert ("A `failure` (`reason` `lecture`) wrote nothing and reads like no "
            "line: the next run is the first to mark, and the invoices fallen "
            "due in between, never listed, are marked too, that day or the "
            "following ones — run the preview again before that run, then "
            "read this line after the run it names.") in step5


def test_the_runbook_never_has_a_later_first_run_mark_all_that_fell_due():
    """Review of 9968616 (B). Past the cap, a later first run marks the
    longest-late first and leaves the newest in `remaining`: « its first run
    is a later night's, which also marks the invoices fallen due in
    between » was false then. Step 4 now says « that day or the following
    ones »."""
    _step3, step4, _step5 = _lot2_steps()
    assert "which also marks the invoices fallen due in between" not in step4
    assert ("its first run is a later day's, and the invoices fallen due in "
            "between are marked too, that day or the following ones (past "
            "the cap, the longest-late go first)") in step4


def test_the_sweep_row_counts_from_the_last_preview_and_reads_no_line():
    """THIRD review of lot 2, and the reviews of 9968616 (A, B, C):
    OBSERVABILITY.md's invoice_overdue_sweep row repeats steps 3 to 5 —
    `marked`, at the first success with the switch serving, is the count of
    the preview's LAST run before it, more by the invoices fallen due in
    between when that preview named an earlier run — except those the cap
    leaves in `remaining` — and by those sent, reopened, unmarked or
    repaired since, less by those paid or voided, or marked « en retard » by
    hand; NO line after a day's 09:00 means its run found the switch off or
    not yet serving (an off run logs nothing), and a `failure` on the
    switch's first run reads the same way. Nothing in it speaks of nights."""
    doc = (_ATHENA / "OBSERVABILITY.md").read_text(encoding="utf-8")
    (row,) = [line for line in doc.splitlines()
              if line.startswith("| `invoice_overdue_sweep` |")]
    assert ("the count of the last run of `scripts/apercu_factures_echues` "
            "before it") in row
    assert "except those the cap leaves in `remaining`" in row
    assert "sent, reopened, unmarked or repaired since" in row
    assert ("less by those paid or voided since, or marked « en retard » by "
            "hand") in row
    assert "No line at all after a day's 09:00" in row
    assert ("On the switch's first run, a `failure` reads like no line: the "
            "next run is the first to mark") in row
    assert "the first night after the switch goes on" not in row
    assert "night" not in row
