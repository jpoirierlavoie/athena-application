"""The main service's appspot block says so — on the REAL app, in its chain.

``main.block_appspot`` (edge layer 3) is a closure inside ``create_app``,
registered after ``security.init_security``'s hooks: its refusal and its
place in the ``before_request`` chain exist only in the real
``main.create_app()``. Built in a subprocess, because importing ``main``
builds the app at module level (root logging handler, OpenTelemetry
provider, Firebase app) — state this suite must not inherit
(``tests/test_provenance.py``).

``appspot_blocked`` was registered with the event registry (2026-04-25) and
emitted by nothing until 2026-11-08: every 403 here was silent. The tests of
the line fail on that code — no line at all; the others pin what did not
change: the status codes, the ``/_ah/`` and ``X-AppEngine-*`` exemptions
(silent), and the chain's order — the origin secret, then the size guard,
then App Check, then this block, so a request refused earlier never reaches
it. The child renders real pages, so it runs behind
tests/_real_app_probe.py's guard: its Firestore client stays a mock, and a
credential lookup or a Google API channel fails the fixture.
"""

import json
import os
import textwrap

import pytest

from tests._real_app_probe import run_probe

APPSPOT = "projet-temoin.nn.r.appspot.com"
SECRET = "s3cret-token-from-the-edge"

# Runs behind tests/_real_app_probe.PRELUDE: a 200 render runs the Réception
# badge (main.inject_reception_badge), which counts the « portail »
# database's invitations — the guard keeps that client a mock.
_PROBE = textwrap.dedent('''
    import json, logging

    import main

    captured = []

    class _Capture(logging.Handler):
        """On the event's own logger, so it sees the record's OWN fields,
        before the root handler's ContextFilter adds the request context."""

        def emit(self, record):
            captured.append({
                "level": record.levelname,
                "fields": dict(getattr(record, "json_fields", {}) or {}),
                "message": record.getMessage(),
            })

    logger = logging.getLogger("pallas.security")
    logger.addHandler(_Capture(level=logging.DEBUG))
    logger.setLevel(logging.DEBUG)

    app = main.app
    APPSPOT = %(appspot)r
    SECRET = %(secret)r
    over = b"Tremblay " * (1024 * 1024 // 9 + 1)
    cases = [
        ("appspot_routed", "GET", "/offline?client=Tremblay&courriel=jean@example.com",
         APPSPOT, {"X-Forwarded-For": "203.0.113.9", "User-Agent": "sonde/1.0"},
         None, ""),
        ("appspot_unrouted", "GET", "/nulle-part/tremblay",
         APPSPOT.upper() + ".", {}, None, ""),
        ("appspot_bare", "GET", "/offline", "appspot.com", {}, None, ""),
        ("warmup", "GET", "/_ah/warmup", APPSPOT, {}, None, ""),
        ("queue", "GET", "/offline", APPSPOT,
         {"X-AppEngine-QueueName": "portail"}, None, ""),
        ("cron", "GET", "/offline", APPSPOT, {"X-Appengine-Cron": "true"},
         None, ""),
        ("edge_host", "GET", "/offline", "athena.example.ca", {}, None, ""),
        ("oversized_on_appspot", "POST", "/offline", APPSPOT, {}, over, ""),
        ("armed_without_header", "GET", "/offline", APPSPOT, {}, None, SECRET),
        ("armed_with_header", "GET", "/offline", APPSPOT,
         {"X-Origin-Auth": SECRET}, None, SECRET),
    ]
    out = {"cases": {}}
    for name, method, path, host, headers, data, secret in cases:
        app.config["CF_ORIGIN_SECRET"] = secret
        captured.clear()
        resp = app.test_client().open(
            path, method=method, data=data, headers={"Host": host, **headers})
        out["cases"][name] = {"status": resp.status_code,
                              "records": list(captured)}
    app.config["CF_ORIGIN_SECRET"] = ""
    out["chain"] = [getattr(f, "__name__", repr(f))
                    for f in app.before_request_funcs[None]]
    print("@@APPSPOT@@" + json.dumps(out))
''') % {"appspot": APPSPOT, "secret": SECRET}


@pytest.fixture(scope="module")
def real_app() -> dict:
    env = {**os.environ}
    # Development mode, explicitly: secrets from the environment, never
    # Secret Manager; the origin check and App Check start unarmed (each
    # case arms the former itself); the required values are test values,
    # whatever the developer's shell holds.
    for name in ("ENV", "CF_ORIGIN_SECRET", "RECAPTCHA_ENTERPRISE_SITE_KEY"):
        env.pop(name, None)
    env.update({
        "SECRET_KEY": "test-secret",
        "FIREBASE_PROJECT_ID": "test-project",
        "FIREBASE_STORAGE_BUCKET": "test-bucket",
        "AUTHORIZED_USER_EMAIL": "test@example.com",
    })
    return run_probe(_PROBE, "@@APPSPOT@@", env=env, timeout=180)


def _events(case: dict, event: str) -> list:
    return [r for r in case["records"] if r["fields"].get("event") == event]


@pytest.mark.parametrize("name, endpoint", [
    ("appspot_routed", "offline"),
    ("appspot_unrouted", ""),
    ("appspot_bare", "offline"),
    ("armed_with_header", "offline"),
])
def test_an_appspot_request_is_refused_with_ONE_line_of_ids(
        real_app, name, endpoint):
    case = real_app["cases"][name]
    assert case["status"] == 403                        # unchanged
    blocked = _events(case, "appspot_blocked")
    assert [r["fields"] for r in blocked] == [
        {"event": "appspot_blocked", "service": "default",
         "endpoint": endpoint}
    ], case["records"]
    assert blocked[0]["level"] == "WARNING"


def test_the_line_carries_no_host_path_query_or_header(real_app):
    for name in ("appspot_routed", "appspot_unrouted", "appspot_bare"):
        case = real_app["cases"][name]
        assert _events(case, "appspot_blocked"), name     # never vacuous
        rendu = json.dumps(case["records"])
        for fuite in ("appspot.com", "APPSPOT", "projet-temoin",
                      "PROJET-TEMOIN", "Tremblay", "tremblay", "jean@",
                      "203.0.113.9", "sonde/1.0", "nulle-part"):
            assert fuite not in rendu, (name, fuite)


@pytest.mark.parametrize("name", ["warmup", "queue", "cron", "edge_host"])
def test_app_engine_s_own_traffic_and_the_edge_host_pass_in_silence(
        real_app, name):
    case = real_app["cases"][name]
    assert case["status"] == 200
    assert _events(case, "appspot_blocked") == []


def test_a_request_the_chain_refuses_EARLIER_never_reaches_the_block(real_app):
    """The order is unchanged, and visible in the lines: an oversized body
    on the appspot host is the size guard's (413, `request_too_large`);
    with the origin secret armed, a request without it is the origin
    check's (403, no line — it refuses on every forged request); only one
    that clears both is this block's."""
    big = real_app["cases"]["oversized_on_appspot"]
    assert big["status"] == 413
    assert _events(big, "appspot_blocked") == []
    assert [r["fields"]["kind"] for r in _events(big, "request_too_large")] == [
        "defaut"]

    armed = real_app["cases"]["armed_without_header"]
    assert armed["status"] == 403
    assert armed["records"] == []


def test_the_before_request_chain_keeps_its_order(real_app):
    chain = real_app["chain"]
    order = ["_enforce_origin_secret", "_enforce_request_size",
             "_verify_app_check", "block_appspot"]
    assert all(chain.count(n) == 1 for n in order), chain
    assert [chain.index(n) for n in order] == sorted(
        chain.index(n) for n in order), chain


def test_the_service_name_is_the_deployment_inventory_s(real_app):
    from utils.deployment_inventory import SERVICE_DEFAULT

    blocked = _events(real_app["cases"]["appspot_routed"], "appspot_blocked")
    assert blocked[0]["fields"]["service"] == SERVICE_DEFAULT
