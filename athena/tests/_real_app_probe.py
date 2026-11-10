"""The guard a REAL-app probe brings into its child process — one source.

Some tests build the real app — ``main.create_app()``, ``client.wsgi`` — in a
subprocess, because importing ``main`` builds the app at module level (root
logging handler, OpenTelemetry provider, Firebase app): state this suite must
not inherit (tests/test_provenance.py). That child loads no conftest, so the
suite's guard (tests/conftest.py) — no channel out, no network, no credential
lookup — does not reach it. Patching ``google.cloud.firestore.Client`` only
around
``import main`` is not enough: a probe that then RENDERED a page built a real
client on the developer's ADC — the Réception badge counts the « portail »
database's invitations — and the fail-open reader hid it (found on 2026-11-09
in tests/test_appspot_block.py, whose child sent that count to production on
every run).

``PRELUDE`` runs first in every such child. It keeps the Firestore client a
mock for the whole process, and refuses — raising in the caller — and
records:

* a credential lookup through ``google.auth.default`` or its private home,
  ``google.auth._default.default``;
* a gRPC channel to anything but this machine, opened through any factory
  of ``CHANNEL_FACTORIES``;
* a DNS lookup, a connect or a datagram to anything but this machine
  (``NETWORK_EVENTS``, through an audit hook): a REST client, a token
  refresh, the metadata server.

Which hosts are this machine, and which events and factories reach out, is
decided here once — ``is_local``, ``is_local_target``, ``audit_host`` — and
tests/conftest.py applies the same rules to the suite's own process. No
proxy stands between them and the host they judge: the child and the suite
run without any proxy variable — gRPC's included — and with ``NO_PROXY=*``
against the Windows registry's proxy. Two reaches no audit event can
refuse in time: a
host NAME handed straight to ``connect()`` or ``sendto()`` is resolved in C
before the event fires (the connect itself is still refused), and Windows'
default asyncio loop connects without any event.

At exit — after the probe's own atexit handlers and non-daemon threads, so a
late reach is not lost — it reports what it refused, and whether each door is
still guarded (a door rebound by the app would disarm the guard silently).
``run_probe`` fails its caller on any such report. The child never sees
``FIRESTORE_EMULATOR_HOST``: a probe reaches no emulator either.
tests/test_real_app_probe.py proves each door bites, and sweeps tests/ for
real-app probes that bypass ``run_probe``.
"""

import ipaddress
import json
import os
import socket
import subprocess
import sys
import textwrap

ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The line the child prints at exit: what its guard refused, as JSON.
GUARD_MARKER = "@@CHILD_GUARD@@"

# Variables a probe child never inherits.
_STRIPPED_ENV = ("FIRESTORE_EMULATOR_HOST",)

# What a probe child, and the suite, run with: no proxy on this machine
# forwards a request the guard would have judged by its real host. requests
# and urllib honour NO_PROXY=*, the Windows registry's proxy included; gRPC
# ignores « * », so the variables it reads are removed instead.
NO_PROXY = {"NO_PROXY": "*", "no_proxy": "*"}
PROXY_VARIABLES = frozenset({"http_proxy", "https_proxy", "all_proxy",
                             "grpc_proxy"})


def without_proxies(env) -> dict:
    """``env`` without any proxy variable, in any case, and with
    ``NO_PROXY=*``."""
    kept = {key: value for key, value in env.items()
            if key.lower() not in PROXY_VARIABLES}
    kept.update(NO_PROXY)
    return kept

# The gRPC channel factories a client opens its channel through, as
# (module, attribute): each one is guarded, in the child and in the suite.
# grpc.experimental.aio keeps its own references to the grpc.aio factories.
CHANNEL_FACTORIES = (
    ("google.api_core.grpc_helpers", "create_channel"),
    ("google.api_core.grpc_helpers_async", "create_channel"),
    ("grpc", "secure_channel"),
    ("grpc", "insecure_channel"),
    ("grpc.aio", "secure_channel"),
    ("grpc.aio", "insecure_channel"),
    ("grpc.experimental.aio", "secure_channel"),
    ("grpc.experimental.aio", "insecure_channel"),
)

# The audit events, raised by Python's socket module, of a DNS lookup, a
# connect or a datagram: each names the host it reaches (``audit_host``).
NETWORK_EVENTS = frozenset({
    "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
    "socket.getnameinfo", "socket.connect", "socket.sendto",
    "socket.sendmsg",
})

# This machine's own name: what getfqdn() — logging, smtplib, an e-mail's
# Message-ID — resolves.
_THIS_HOST = socket.gethostname().lower()


def is_local(host: str) -> bool:
    """``host`` — lowercase, as given — is this machine: no host at all,
    ``localhost``, this machine's own name, a loopback or unspecified
    address (IPv4-mapped, or with an IPv6 zone). Anything else is outside:
    a private address, a trailing dot (a name to the resolver), a ``%``
    spelling on IPv4."""
    if host in ("", "localhost", _THIS_HOST):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback or address.is_unspecified


def is_local_target(target) -> bool:
    """A gRPC target — ``host:port``, ``[v6]:port``, ``dns:///host`` — that
    stays on this machine; a Unix socket always does. The host is judged
    before any ``?`` or ``#``, and a ``%`` — gRPC decodes it — is outside."""
    target = str(target)
    if target.startswith(("unix:", "unix-abstract:")):
        return True
    host = target.split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1]
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.rsplit(":", 1)[0]
    return "%" not in host and is_local(host.lower())


def audit_host(event: str, args) -> str | None:
    """The host a network audit event reaches, lowercase; None for any
    other event, or for an address that is no host (a Unix socket's path, a
    datagram sent on a connected socket)."""
    if event in ("socket.getaddrinfo", "socket.gethostbyname",
                 "socket.gethostbyaddr"):
        host = args[0]
    elif event == "socket.getnameinfo" and isinstance(args[0], tuple):
        host = args[0][0]
    elif (event in ("socket.connect", "socket.sendto", "socket.sendmsg")
            and isinstance(args[1], tuple) and args[1]):
        host = args[1][0]
    else:
        return None
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", "replace")
    return str(host or "").lower()


PRELUDE = textwrap.dedent('''
    import atexit, importlib, json, os, sys
    from unittest import mock
    sys.path.insert(0, os.getcwd())
    from tests._real_app_probe import (
        CHANNEL_FACTORIES as _child_guard_factories,
        NETWORK_EVENTS as _child_guard_events,
        audit_host as _child_guard_audit_host,
        is_local as _child_guard_is_local,
        is_local_target as _child_guard_is_local_target,
    )

    _CHILD_GUARD_REACHED = []
    _CHILD_GUARD_DOORS = {}


    def _child_guard_no_credentials(*args, **kwargs):
        _CHILD_GUARD_REACHED.append("google.auth.default")
        raise RuntimeError("a probe child never looks up credentials")


    def _child_guard_channel(real):
        def guarded(target, *args, **kwargs):
            if not _child_guard_is_local_target(target):
                _CHILD_GUARD_REACHED.append(str(target))
                raise RuntimeError("a probe child never opens a channel out")
            return real(target, *args, **kwargs)
        return guarded


    def _child_guard_audit(event, args):
        if event not in _child_guard_events:
            return
        host = _child_guard_audit_host(event, args)
        if host is not None and not _child_guard_is_local(host):
            _CHILD_GUARD_REACHED.append("network " + host)
            raise RuntimeError("a probe child never opens the network")


    import google.auth
    import google.auth._default

    for _home, _name in _child_guard_factories:
        _owner = importlib.import_module(_home)
        _guarded = _child_guard_channel(getattr(_owner, _name))
        setattr(_owner, _name, _guarded)
        _CHILD_GUARD_DOORS[_home + "." + _name] = (_owner, _name, _guarded)
    for _owner, _door in ((google.auth, "google.auth.default"),
                          (google.auth._default, "google.auth._default.default")):
        _owner.default = _child_guard_no_credentials
        _CHILD_GUARD_DOORS[_door] = (_owner, "default", _child_guard_no_credentials)
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
    child_env = without_proxies(
        {k: v for k, v in env.items() if k not in _STRIPPED_ENV})
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
        "a real-app probe reached credentials, the network or a channel out, "
        "or disarmed its guard — keep its Firestore client a mock: %r"
        % (reached,))
    line = next(l for l in proc.stdout.splitlines() if l.startswith(marker))
    return json.loads(line[len(marker):])
