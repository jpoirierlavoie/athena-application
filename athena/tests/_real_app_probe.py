"""The guard a REAL-app probe brings into its child process — one source.

Some tests build the real app — ``main.create_app()``, ``client.wsgi`` — in a
subprocess, because importing ``main`` builds the app at module level (root
logging handler, OpenTelemetry provider, Firebase app): state this suite must
not inherit (tests/test_provenance.py). That child loads no conftest, so the
suite's guard — no gRPC channel to a Google API (tests/conftest.py) — does not
reach it. Patching ``google.cloud.firestore.Client`` only around
``import main`` is not enough: a probe that then RENDERED a page built a real
client on the developer's ADC — the Réception badge counts the « portail »
database's invitations — and the fail-open reader hid it (found on 2026-11-09
in tests/test_appspot_block.py, whose child sent that count to production on
every run).

``PRELUDE`` runs first in every such child. It keeps the Firestore client a
mock for the whole process, and refuses — raising in the caller — and
records:

* a credential lookup through ``google.auth.default``;
* a gRPC channel to anything but loopback, opened through
  ``google.api_core.grpc_helpers`` (sync or async) or ``grpc`` itself (sync
  or ``grpc.aio``), a host with a trailing dot included;
* a DNS lookup or a socket connect to anything but loopback (an audit hook):
  a REST client, the metadata server.

At exit — after the probe's own atexit handlers and non-daemon threads, so a
late reach is not lost — it reports what it refused, and whether each door is
still guarded (a door rebound by the app would disarm the guard silently).
``run_probe`` fails its caller on any such report. The child never sees
``FIRESTORE_EMULATOR_HOST``: a probe reaches no emulator either.
tests/test_real_app_probe.py proves each door bites, and sweeps tests/ for
real-app probes that bypass ``run_probe``.
"""

import json
import os
import subprocess
import sys
import textwrap

ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The line the child prints at exit: what its guard refused, as JSON.
GUARD_MARKER = "@@CHILD_GUARD@@"

# Variables a probe child never inherits.
_STRIPPED_ENV = ("FIRESTORE_EMULATOR_HOST",)

PRELUDE = textwrap.dedent('''
    import atexit, json, os, sys
    from unittest import mock
    sys.path.insert(0, os.getcwd())

    _CHILD_GUARD_REACHED = []
    _CHILD_GUARD_DOORS = {}


    def _child_guard_host(target):
        host = str(target).rsplit("/", 1)[-1]
        if host.startswith("["):
            host = host[1:].split("]", 1)[0]
        elif host.count(":") == 1:
            host = host.rsplit(":", 1)[0]
        return host.lower().rstrip(".")


    def _child_guard_is_local(host):
        return (host in ("", "localhost", "::1", "::", "0.0.0.0")
                or host.startswith("127."))


    def _child_guard_no_credentials(*args, **kwargs):
        _CHILD_GUARD_REACHED.append("google.auth.default")
        raise RuntimeError("a probe child never looks up credentials")


    def _child_guard_channel(real):
        def guarded(target, *args, **kwargs):
            if not _child_guard_is_local(_child_guard_host(target)):
                _CHILD_GUARD_REACHED.append(str(target))
                raise RuntimeError("a probe child never opens a channel out")
            return real(target, *args, **kwargs)
        return guarded


    def _child_guard_audit(event, args):
        if event == "socket.getaddrinfo":
            host = args[0]
        elif (event == "socket.connect" and isinstance(args[1], tuple)
                and args[1]):
            host = args[1][0]
        else:
            return
        if isinstance(host, bytes):
            host = host.decode("ascii", "replace")
        host = str(host or "").lower().rstrip(".")
        if not _child_guard_is_local(host):
            _CHILD_GUARD_REACHED.append("network " + host)
            raise RuntimeError("a probe child never opens the network")


    import grpc
    import grpc.aio
    import google.auth
    from google.api_core import grpc_helpers as _child_guard_grpc
    from google.api_core import grpc_helpers_async as _child_guard_grpc_async

    for _owner, _name in ((_child_guard_grpc, "create_channel"),
                          (_child_guard_grpc_async, "create_channel"),
                          (grpc, "secure_channel"),
                          (grpc, "insecure_channel"),
                          (grpc.aio, "secure_channel"),
                          (grpc.aio, "insecure_channel")):
        _guarded = _child_guard_channel(getattr(_owner, _name))
        setattr(_owner, _name, _guarded)
        _CHILD_GUARD_DOORS[_owner.__name__ + "." + _name] = (_owner, _name, _guarded)
    google.auth.default = _child_guard_no_credentials
    _CHILD_GUARD_DOORS["google.auth.default"] = (
        google.auth, "default", _child_guard_no_credentials)
    mock.patch("google.cloud.firestore.Client").start()


    def _child_guard_report():
        for _door, (_owner, _name, _guarded) in _CHILD_GUARD_DOORS.items():
            if getattr(_owner, _name, None) is not _guarded:
                _CHILD_GUARD_REACHED.append("guard disarmed: " + _door)
        _firestore = sys.modules.get("google.cloud.firestore")
        if _firestore is not None and not isinstance(_firestore.Client, mock.Mock):
            _CHILD_GUARD_REACHED.append("guard disarmed: google.cloud.firestore.Client")
        print("@@CHILD_GUARD@@" + json.dumps(_CHILD_GUARD_REACHED))
        sys.stdout.flush()


    # Registered FIRST, so it runs LAST: after the probe's own atexit
    # handlers, and after the interpreter has joined its non-daemon threads.
    atexit.register(_child_guard_report)
    sys.addaudithook(_child_guard_audit)
''')


def run_child(body: str, *, env: dict, timeout: int):
    """Run ``body`` (top-level Python source) in a fresh interpreter behind
    ``PRELUDE``, from athena/. Returns the process and what the guard
    refused or found disarmed — the list itself, never judged here."""
    child_env = {k: v for k, v in env.items() if k not in _STRIPPED_ENV}
    proc = subprocess.run(
        [sys.executable, "-c", PRELUDE + "\n" + body + "\n"],
        cwd=ATHENA, env=child_env, capture_output=True, text=True,
        encoding="utf-8", timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    guard = [l for l in proc.stdout.splitlines() if l.startswith(GUARD_MARKER)]
    assert len(guard) == 1, ("the child's guard did not report at exit",
                             proc.stderr[-3000:])
    return proc, json.loads(guard[0][len(GUARD_MARKER):])


def run_probe(body: str, marker: str, *, env: dict, timeout: int):
    """Run a real-app probe behind the guard and return the JSON it printed
    after ``marker``. Fails the caller when the child reached anything the
    guard refuses, or disarmed one of its doors: a real client there runs on
    the developer's ADC — against production — or on the build's account."""
    proc, reached = run_child(body, env=env, timeout=timeout)
    assert reached == [], (
        "a real-app probe reached credentials, the network or a Google API, "
        "or disarmed its guard — keep its Firestore client a mock: %r"
        % (reached,))
    line = next(l for l in proc.stdout.splitlines() if l.startswith(marker))
    return json.loads(line[len(marker):])
