"""The App Engine warmup primes what a worker's first page pays for (2026-11-12).

``/_ah/warmup`` used to open the default Firestore channel only. It now also
opens the « portail » channel (every page's Réception badge reads it),
imports the two Firebase modules loaded on first use, and compiles the two
templates every first page renders — and still answers 200 when any step
fails. The route is a closure inside ``main.create_app()``, so it runs in a
subprocess behind tests/_real_app_probe.py's guard (its Firestore client is
replaced here, and a credential lookup or a channel out fails the fixture).
"""

import os
import textwrap

import pytest

from tests._real_app_probe import run_probe

_PROBE = textwrap.dedent('''
    import json

    import main
    import models
    from models import portail_invitation

    app = main.app
    calls, templates = [], []

    class _Doc:
        def __init__(self, path):
            self.path = path

        def get(self):
            calls.append(self.path)

    class _Col:
        def __init__(self, name):
            self.name = name

        def document(self, doc_id):
            return _Doc(self.name + "/" + doc_id)

    class _Db:
        def collection(self, name):
            return _Col(name)

    def _down(*_a, **_k):
        raise RuntimeError("down")

    real_get_template = app.jinja_env.get_template

    def _get_template(name, *a, **k):
        templates.append(name)
        return real_get_template(name, *a, **k)

    models.db = _Db()
    portail_invitation._col = lambda: _Col("invitations")
    app.jinja_env.get_template = _get_template
    out = {}
    resp = app.test_client().get("/_ah/warmup", headers={"Host": "athena.example.ca"})
    out["ok"] = {"status": resp.status_code, "calls": calls, "templates": templates}

    models.db = type("_Boom", (), {"collection": _down})()
    portail_invitation._col = _down
    app.jinja_env.get_template = _down
    resp = app.test_client().get("/_ah/warmup", headers={"Host": "athena.example.ca"})
    out["failing"] = {"status": resp.status_code, "body": resp.get_data(as_text=True)}
    print("@@WARMUP@@" + json.dumps(out))
''')


@pytest.fixture(scope="module")
def warmup() -> dict:
    env = {**os.environ}
    for name in ("ENV", "CF_ORIGIN_SECRET", "RECAPTCHA_ENTERPRISE_SITE_KEY"):
        env.pop(name, None)
    env.update({
        "SECRET_KEY": "test-secret",
        "FIREBASE_PROJECT_ID": "test-project",
        "FIREBASE_STORAGE_BUCKET": "test-bucket",
        "AUTHORIZED_USER_EMAIL": "test@example.com",
    })
    return run_probe(_PROBE, "@@WARMUP@@", env=env, timeout=180)


def test_the_warmup_opens_both_firestore_channels_and_compiles_the_first_templates(warmup):
    ok = warmup["ok"]
    assert ok["status"] == 200
    assert ok["calls"] == ["dav_sync/parties", "invitations/_warmup"]
    assert ok["templates"] == ["base.html", "dashboard/index.html"]


def test_a_failing_step_never_fails_the_warmup(warmup):
    assert warmup["failing"] == {"status": 200, "body": ""}
