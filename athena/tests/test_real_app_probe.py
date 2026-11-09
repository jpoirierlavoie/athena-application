"""The child-process guard of tests/_real_app_probe.py bites, and the
real-app probes of the suite go through it.

A probe that imports ``main`` or ``client.wsgi`` in a subprocess runs outside
conftest's guard. tests/_real_app_probe.PRELUDE keeps its Firestore client a
mock for the whole child and refuses credential lookups, outbound channels and
network access; ``run_probe`` fails the caller on any of them, or on a door
the child disarmed. These tests try every door in a child — the leak of
2026-11-09 included, a client a render built once the Firestore patch had
stopped — and sweep tests/ for real-app probes that bypass ``run_probe``.
The sweep runs on the real files and on a planted tree. Every door is tried
offline: the guard raises before any packet leaves.
"""

import ast
import os
import pathlib
import re
import sys
import textwrap
import warnings

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests._real_app_probe import (  # noqa: E402
    GUARD_MARKER,
    PRELUDE,
    run_child,
    run_probe,
)

TESTS = pathlib.Path(__file__).resolve().parent
_ENV = {k: v for k, v in os.environ.items() if k != "ENV"}
_SELF = ("_real_app_probe.py", "test_real_app_probe.py")


def _reached(body: str) -> list:
    _proc, reached = run_child(body, env=_ENV, timeout=120)
    return reached


def test_the_child_reports_with_the_helpers_marker():
    assert GUARD_MARKER in PRELUDE


def test_the_guard_refuses_and_records_both_classic_doors():
    """A credential lookup and a gRPC channel through google.api_core — the
    doors the 2026-11-09 leak went through — refused (the call raises) and
    recorded, in order."""
    body = (
        "import google.auth\n"
        "from google.api_core import grpc_helpers\n"
        "for door in (lambda: google.auth.default(),\n"
        "             lambda: grpc_helpers.create_channel("
        "'firestore.googleapis.com:443')):\n"
        "    try:\n"
        "        door()\n"
        "    except RuntimeError:\n"
        "        pass\n"
    )
    assert _reached(body) == ["google.auth.default",
                              "firestore.googleapis.com:443"]


@pytest.mark.parametrize("door, recorded", [
    ("from google.api_core import grpc_helpers_async\n"
     "grpc_helpers_async.create_channel('firestore.googleapis.com:443')",
     "firestore.googleapis.com:443"),
    ("import grpc\ngrpc.secure_channel('firestore.googleapis.com:443', "
     "grpc.ssl_channel_credentials())", "firestore.googleapis.com:443"),
    ("import grpc\ngrpc.insecure_channel('example.org:50051')",
     "example.org:50051"),
    ("import grpc.aio\ngrpc.aio.insecure_channel('example.org:50051')",
     "example.org:50051"),
    ("from google.api_core import grpc_helpers\n"
     "grpc_helpers.create_channel('firestore.googleapis.com.:443')",
     "firestore.googleapis.com.:443"),
    ("import socket\nsocket.getaddrinfo('storage.googleapis.com', 443)",
     "network storage.googleapis.com"),
    ("import socket\nsocket.create_connection(('metadata.google.internal', 80),"
     " timeout=1)", "network metadata.google.internal"),
])
def test_every_outbound_door_is_refused_and_recorded(door, recorded):
    """Beyond the classic doors: an async or raw gRPC channel, a host with a
    trailing dot, a DNS lookup or a socket connect — what a REST client or
    the metadata server would need."""
    body = "try:\n" + textwrap.indent(door, "    ") + "\nexcept RuntimeError:\n    pass\n"
    assert _reached(body) == [recorded]


def test_loopback_stays_open():
    """A local server stays reachable: a refusal is for the outside only."""
    body = (
        "import grpc, socket\n"
        "grpc.insecure_channel('127.0.0.1:50051').close()\n"
        "socket.getaddrinfo('localhost', 80)\n"
    )
    assert _reached(body) == []


def test_the_firestore_client_stays_a_mock_for_the_whole_child():
    """Not only around an import: a client built AFTER it — what a rendered
    page builds — is the mock too, and reaches nothing."""
    body = (
        "from google.cloud import firestore\n"
        "client = firestore.Client(database='portail')\n"
        "print('@@T@@' + json.dumps(type(client).__module__))\n"
    )
    assert run_probe(body, "@@T@@", env=_ENV, timeout=120) == "unittest.mock"


def test_a_real_client_built_once_the_patch_stops_fails_the_probe():
    """The leak of 2026-11-09, planted: the patch stopped after the import,
    then a client built as the Réception badge builds it. The guard refuses
    the credential lookup — nothing reaches Google — and run_probe fails. An
    exported FIRESTORE_EMULATOR_HOST (CLAUDE.md's local setup) changes
    nothing: the child never inherits it."""
    body = (
        "mock.patch.stopall()\n"
        "from google.cloud import firestore\n"
        "try:\n"
        "    firestore.Client(database='portail')\n"
        "except Exception:\n"
        "    pass\n"
        "print('@@T@@' + json.dumps('built'))\n"
    )
    for env in (_ENV, {**_ENV, "FIRESTORE_EMULATOR_HOST": "127.0.0.1:9"}):
        with pytest.raises(AssertionError) as raised:
            run_probe(body, "@@T@@", env=env, timeout=120)
        assert "google.auth.default" in str(raised.value)


def test_the_child_never_sees_the_emulator_variable():
    body = "print('@@T@@' + json.dumps(os.environ.get('FIRESTORE_EMULATOR_HOST')))\n"
    env = {**_ENV, "FIRESTORE_EMULATOR_HOST": "127.0.0.1:9"}
    assert run_probe(body, "@@T@@", env=env, timeout=120) is None


@pytest.mark.parametrize("late", [
    "import atexit, google.auth\n"
    "atexit.register(lambda: google.auth.default())\n",
    "import threading, time, google.auth\n"
    "def later():\n"
    "    time.sleep(0.3)\n"
    "    try:\n"
    "        google.auth.default()\n"
    "    except RuntimeError:\n"
    "        pass\n"
    "threading.Thread(target=later).start()\n",
])
def test_a_reach_after_the_body_is_still_reported(late):
    """An atexit handler or a non-daemon thread of the probe reaches after
    its body ends: the report runs last, so it is not lost."""
    body = late + "print('@@T@@' + json.dumps('done'))\n"
    with pytest.raises(AssertionError) as raised:
        run_probe(body, "@@T@@", env=_ENV, timeout=120)
    assert "google.auth.default" in str(raised.value)


@pytest.mark.parametrize("rebind, door", [
    ("import google.auth, google.auth._default\n"
     "google.auth.default = google.auth._default.default\n",
     "google.auth.default"),
    ("from google.api_core import grpc_helpers\n"
     "grpc_helpers.create_channel = lambda *a, **k: None\n",
     "google.api_core.grpc_helpers.create_channel"),
    ("mock.patch.stopall()\nfrom google.cloud import firestore\n",
     "google.cloud.firestore.Client"),
])
def test_a_door_the_child_disarms_is_reported(rebind, door):
    """Code that rebinds a door — a dependency at import, a page at render —
    would switch the guard off silently: the exit report checks every door."""
    body = rebind + "print('@@T@@' + json.dumps('done'))\n"
    with pytest.raises(AssertionError) as raised:
        run_probe(body, "@@T@@", env=_ENV, timeout=120)
    assert "guard disarmed: " + door in str(raised.value)


def test_the_probe_marker_json_round_trips():
    """run_probe returns what the body printed after its marker, decoded."""
    body = "print('@@T@@' + json.dumps({'a': [1, 'é']}))\n"
    assert run_probe(body, "@@T@@", env=_ENV, timeout=120) == {"a": [1, "é"]}


# ── The real-app probes go through run_probe ────────────────────────────

# A line that imports the real app — `import main`, `from main import app`,
# `import client.wsgi`, `from client.app import create_portail_app` — read
# from a string constant: by Python's own parser when the constant parses,
# by this linear regex when it does not (a %-template).
_ROOTS = ("main", "client")
_REAL_APP_IMPORT = re.compile(
    r"^[ \t]*(?:import (?:main|client)\b|from (?:main|client)(?:\.\w+)* import\b)",
    re.MULTILINE)


def _names_the_app(name) -> bool:
    return isinstance(name, str) and name.split(".")[0] in _ROOTS


def _imports_the_app(text: str) -> bool:
    if _REAL_APP_IMPORT.search(text):
        return True
    try:
        # Most constants are prose or regexes, not code: their `\s` would
        # each raise a SyntaxWarning, which says nothing about a probe.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(textwrap.dedent(text))
    except (SyntaxError, ValueError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
                _names_the_app(a.name) for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.level == 0 and \
                _names_the_app(node.module):
            return True
        if isinstance(node, ast.Call) and node.args and isinstance(
                node.args[0], ast.Constant):
            func = node.func
            called = (func.id if isinstance(func, ast.Name)
                      else func.attr if isinstance(func, ast.Attribute) else "")
            if called in ("import_module", "__import__", "run_module") and \
                    _names_the_app(node.args[0].value):
                return True
    return False


def holds_a_probe(tree: ast.AST) -> bool:
    return any(isinstance(n, ast.Constant) and isinstance(n.value, str)
               and _imports_the_app(n.value) for n in ast.walk(tree))


def probe_problems(root: pathlib.Path) -> list:
    """Modules under ``root`` holding a real-app probe that do not run it
    through ``run_probe``, or that import ``subprocess`` — so start a child
    themselves, however the interpreter is named."""
    problems = []
    for path in sorted(root.rglob("*.py")):
        if path.name in _SELF or "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not holds_a_probe(tree):
            continue
        name = path.relative_to(root).as_posix()
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
        if not any((isinstance(c.func, ast.Name) and c.func.id == "run_probe")
                   or (isinstance(c.func, ast.Attribute)
                       and c.func.attr == "run_probe") for c in calls):
            problems.append(name + ": a real-app probe outside run_probe")
        if any((isinstance(n, ast.Import)
                and any(a.name == "subprocess" for a in n.names))
               or (isinstance(n, ast.ImportFrom) and n.module == "subprocess")
               for n in ast.walk(tree)):
            problems.append(name + ": starts a Python child itself")
    return problems


def test_every_real_app_probe_of_the_suite_goes_through_run_probe():
    """Not vacuous: the predicate itself must see today's probes."""
    seen = {p.name for p in TESTS.rglob("*.py")
            if p.name not in _SELF and "__pycache__" not in p.parts
            and holds_a_probe(ast.parse(p.read_text(encoding="utf-8")))}
    assert {"test_appspot_block.py", "test_post_refusals.py",
            "test_provenance.py"} <= seen, seen
    assert probe_problems(TESTS) == []


def test_the_probe_sweep_catches_a_planted_unguarded_probe(tmp_path):
    (tmp_path / "test_guarded.py").write_text(
        "PROBE = '''\nimport main\n'''\n"
        "def test_x():\n    run_probe(PROBE, '@@', env={}, timeout=1)\n",
        encoding="utf-8")
    (tmp_path / "test_bare.py").write_text(
        "import subprocess, sys\n"
        "PROBE = '''\nfrom unittest import mock\n"
        "with mock.patch('google.cloud.firestore.Client'):\n"
        "    import main\n'''\n"
        "def test_x():\n"
        "    subprocess.run([sys.executable, '-c', PROBE])\n",
        encoding="utf-8")
    (tmp_path / "test_portal.py").write_text(
        "PROBE = 'import client.wsgi as portal'\n", encoding="utf-8")
    (tmp_path / "test_from_main.py").write_text(
        "PROBE = '''\nfrom main import app\n'''\n", encoding="utf-8")
    (tmp_path / "test_factory.py").write_text(
        "PROBE = '''\n    from client.app import create_portail_app\n'''\n",
        encoding="utf-8")
    (tmp_path / "test_dynamic.py").write_text(
        "PROBE = \"import importlib\\napp = importlib.import_module('main').app\"\n",
        encoding="utf-8")
    (tmp_path / "test_compound.py").write_text(
        "PROBE = 'import os, main'\n", encoding="utf-8")
    sub = tmp_path / "nested"
    sub.mkdir()
    (sub / "test_deep.py").write_text(
        "from subprocess import run\n"
        "PROBE = 'import sys; import main'\n"
        "def test_x():\n    run_probe(PROBE, '@@', env={}, timeout=1)\n",
        encoding="utf-8")
    (tmp_path / "test_unrelated.py").write_text(
        "import subprocess, sys\n"
        "TEXT = 'the main import of this module'\n"
        "def test_x():\n"
        "    subprocess.run([sys.executable, '-c', 'print(1)'])\n",
        encoding="utf-8")
    assert probe_problems(tmp_path) == [
        "nested/test_deep.py: starts a Python child itself",
        "test_bare.py: a real-app probe outside run_probe",
        "test_bare.py: starts a Python child itself",
        "test_compound.py: a real-app probe outside run_probe",
        "test_dynamic.py: a real-app probe outside run_probe",
        "test_factory.py: a real-app probe outside run_probe",
        "test_from_main.py: a real-app probe outside run_probe",
        "test_portal.py: a real-app probe outside run_probe",
    ]
