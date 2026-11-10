"""Suite-wide isolation: no test module leaves the production log chain on
the root logger (finitions, E3 — the flake hunt).

``utils.logging_setup.init_app`` REMOVES every root handler, installs the
production one (ContextFilter + RedactionFilter) and sets the root level to
DEBUG. ``tests/test_logging_setup.py`` calls it directly, and
``client.app.create_portail_app`` calls it from the module-scoped app
fixtures of ``tests/test_portail_*.py``. Nothing restored the root logger,
so every LATER module logged through that chain — and ``RedactionFilter``
rewrites a record IN PLACE (it renders ``exc_info`` into a scrubbed
``exc_text`` and clears ``exc_info``) before ``caplog``'s handler sees it.
A test asserting ``record.exc_info`` then passed or failed by the order of
the files: run in reverse (or ``test_logging_setup.py`` /
``test_portail_app.py`` before ``test_dav_root_discovery.py``), four tests
of the latter failed, deterministically — green in the default order only
because « d » sorts before « l » and « p ».

The cleanup is MODULE-scoped on purpose: the portal fixtures build their
app once per module, and a module may rely on the chain it installed; what
must never happen is that the NEXT module inherits it. It removes only the
handlers carrying the production RedactionFilter — never pytest's own
capture handlers, which pytest adds and removes around every phase — and
puts the root level back. ``tests/test_suite_isolation.py`` pins it by
running the failing order in a subprocess.

No test reaches a Google API either. ``models/__init__.py`` builds
``firestore.Client()`` at import, so a module that imports ``models`` without
patching it first gets a REAL client — and on a workstation whose ADC is set,
a real client reads PRODUCTION Firestore. Which module imports ``models``
first depended on the selection: the full run's first importer patched it
(``models.db`` a MagicMock), but a targeted run of one of eleven unpatched
modules got the real client for the whole session (``test_budget`` then read
``settings/cabinet``, and eleven ``test_mcp_output_schemas`` tests ran their
reads, until 2026-11-06). So this file imports ``models`` once, under the
same patch the test modules use: every selection starts where the full run
does.
And as a backstop, a gRPC client opens its channel through a factory of
``tests/_real_app_probe.CHANNEL_FACTORIES`` on its first RPC: a target
outside this machine is refused there AND recorded, with the line behind
it, because the readers that hit it fail open — they swallow the error — so
only the check below fails the test.

Nor does a test reach the network. An audit hook refuses — with a
``RuntimeError``, which no retry loop waits on — and records, with the line
behind it, a DNS lookup, a connect or a datagram made in Python to anything
but this machine (``NETWORK_EVENTS``): what a REST client, a token refresh
or the metadata server would need. What is this machine is decided once, in
``tests/_real_app_probe.is_local``, for this process and for the real-app
children alike, and the session runs without any proxy variable and with
``NO_PROXY=*``, so that no proxy on this machine forwards a request the
hook judged local, gRPC's included. gRPC resolves and connects below
Python: the channel guard covers it. What no event can refuse in time is
named in tests/_real_app_probe.py.

Nor does a test look up credentials. ``google.auth.default`` reads the
developer's ADC and, for a user account, runs ``gcloud config get project``
— on Cloud Build, it takes the build's own account — before any client
exists; until 2026-11-09 a tracing test did it on every run without a single
RPC, so the channel guard never saw it. It is refused — under both of its
names, the public one and its private home ``google.auth._default.default``
— with the ``DefaultCredentialsError`` callers already expect when there is
none, and recorded with the innermost line of this repository's own code
behind the call (never a library's, nor a virtualenv's kept inside athena/)
— when there is none, the innermost line no library holds, else the guard's
direct caller; and the same check fails the test. A test that needs
credentials stubs them — the way tests/test_tracing_setup.py stubs
``opentelemetry.gcp_credential_provider``. A test's own monkeypatch of
``google.auth.default`` wins for that test, and is undone after it.

Every guard is live before this file imports ``models``, so that import
chain meets them too. The check runs once a test's whole teardown is over
(``pytest_runtest_teardown``): what a class, module or package fixture
records as it ends counts against the test it ends with. It drains every
record at once; a record a thread adds meanwhile, or a fixture value's
finalizer as pytest drops it after the check, waits for the next check, and
so does one an interrupted teardown leaves under ``--pdb``, where pytest
carries on. Whatever the plugins inside make of that teardown — the xfail
plugin turns any exception into an expected failure, the unittest plugin can
put a TestCase's leftover outcome in its place — the guard's failure is
reported as a failure, with its message (``pytest_runtest_makereport``).
What no test owns — collection, where the test modules' top-level code and
this file's own import of ``models`` run, and what an interrupted run tears
down after its last test — fails the session at its end and blames no test
(``pytest_sessionfinish``). Last, at exit — after the interpreter has joined
the tests' non-daemon threads and run the atexit handlers of ``models`` and
of the tests — a reach that late fails the process, with status 1 whatever
pytest returned (``_after_the_session``).

A child pytest that runs this suite's files, or loads this file with ``-p``,
carries these guards. A child that runs the real app carries its own prelude
instead (tests/_real_app_probe.py), which refuses the same doors by the same
rules — the credential lookup with a ``RuntimeError`` there — and reports at
exit a door the app rebound. Any other child carries none.
"""

import atexit
import importlib
import logging
import os
import sys
from unittest import mock

import google.auth
import google.auth._default
import google.auth.exceptions
import pytest

from tests._real_app_probe import (
    CHANNEL_FACTORIES,
    NETWORK_EVENTS,
    NO_PROXY,
    PROXY_VARIABLES,
    audit_host,
    is_local,
    is_local_target,
)


def _without_proxies() -> None:
    """No proxy on this machine forwards a request the network guard would
    have judged by its real host: every client connects directly, gRPC's
    included."""
    for name in [name for name in os.environ
                 if name.lower() in PROXY_VARIABLES]:
        del os.environ[name]
    os.environ.update(NO_PROXY)


_without_proxies()

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ATHENA)


def _after_the_session() -> None:
    """At exit: what nothing else can report any more fails the process."""
    problems = [f"during collection, {problem}"
                for problem in _drain(_COLLECTION_REACHES)]
    problems += [f"at exit, {problem}" for problem in _pending_reaches()]
    if problems:
        sys.stdout.flush()
        sys.stderr.write("".join(f"{problem}\n" for problem in problems))
        sys.stderr.flush()
        os._exit(1)


# Before models and every test module, so that it runs after their atexit
# handlers (only logging's, registered when pytest imported it, runs later:
# it reaches nothing).
atexit.register(_after_the_session)

_CHANNEL_TARGETS: list[str] = []
_NETWORK_REACHES: list[str] = []
_CREDENTIAL_LOOKUPS: list[str] = []


def _guarded_channel(real):
    def guarded(target, *args, **kwargs):
        if not is_local_target(target):
            _CHANNEL_TARGETS.append(f"{target} ({_lookup_site()})")
            raise RuntimeError(f"tests never open a channel out ({target})")
        return real(target, *args, **kwargs)
    return guarded


def _guard_the_channel_factories() -> None:
    for home, name in CHANNEL_FACTORIES:
        owner = importlib.import_module(home)
        setattr(owner, name, _guarded_channel(getattr(owner, name)))


def _guarded_network(event, args) -> None:
    # An audit hook sees every audited event of the process: this test first.
    if event not in NETWORK_EVENTS:
        return
    host = audit_host(event, args)
    if host is None or is_local(host):
        return
    _NETWORK_REACHES.append(f"{host} ({_lookup_site()})")
    raise RuntimeError(f"tests never reach the network ({host})")


def _within(path: str, root: str) -> bool:
    return os.path.normcase(path).startswith(os.path.normcase(root) + os.sep)


def _envs_in(root: str, prefixes) -> list[str]:
    """The environments among ``prefixes`` kept inside ``root``."""
    return sorted({os.path.abspath(prefix) for prefix in prefixes
                   if _within(os.path.abspath(prefix), root)})


# The running virtualenv when it is kept inside athena/ (a developer's
# .venv): its launcher and its libraries are not this repository's code.
_ENVS_IN_ATHENA = _envs_in(_ATHENA, (sys.prefix, sys.exec_prefix))


# Where the interpreter's own files live — the standard library, a
# virtualenv's launchers — wherever they are.
_PYTHON_HOMES = sorted({os.path.abspath(home) for home in (
    os.path.dirname(os.__file__), sys.prefix, sys.exec_prefix,
    sys.base_prefix, sys.base_exec_prefix)})


def _place(path: str) -> tuple[str, str]:
    """A frame's file, shortened — relative to athena/ or to site-packages,
    else its bare name, never a home directory — and whose code it is:
    « ours » (this repository's), « library » (site-packages, the
    interpreter's own files, a virtualenv kept inside athena/, code with no
    file at all) or « other » (a test file written outside the
    repository)."""
    if path.startswith("<"):  # <frozen runpy>, <string>: no file at all
        return path, "library"
    path = os.path.abspath(path)
    parts = path.replace("\\", "/").split("/")
    for packages in ("site-packages", "dist-packages"):
        if packages in parts:
            last = len(parts) - 1 - parts[::-1].index(packages)
            return "/".join(parts[last + 1:]), "library"
    if _within(path, _ATHENA):
        short = os.path.relpath(path, _ATHENA).replace("\\", "/")
        in_an_env = any(_within(path, env) for env in _ENVS_IN_ATHENA)
        return short, "library" if in_an_env else "ours"
    python = any(_within(path, home) for home in _PYTHON_HOMES)
    return os.path.basename(path), "library" if python else "other"


def _lookup_site() -> str:
    """Where the reach came from: the innermost line of this repository's
    own code (the app or test line behind it), else the innermost line no
    library holds (a test written outside the repository), else the guard's
    direct caller, else « ? » — a thread started on the guard itself has no
    Python caller at all."""
    frame = sys._getframe(1).f_back  # the guard's caller, or None
    caller = other = None
    while frame is not None:
        short, kind = _place(frame.f_code.co_filename)
        site = f"{short}:{frame.f_lineno}"
        caller = caller or site
        if kind == "ours" and short != "tests/conftest.py":
            return site
        if kind == "other":
            other = other or site
        frame = frame.f_back
    return other or caller or "?"


def _guarded_default(*args, **kwargs):
    _CREDENTIAL_LOOKUPS.append(_lookup_site())
    raise google.auth.exceptions.DefaultCredentialsError(
        "tests never look up credentials (tests/conftest.py)")


_COLLECTION_REACHES: list[str] = []
# The guard's own failure of a test's teardown, by node id, until its report.
_FAILED_BY_THE_GUARD: dict[str, BaseException] = {}


def _drain(records: list[str]) -> list[str]:
    """What the list holds, taken out of it: a record a thread adds
    meanwhile stays for the next check (a copy, then a deletion of exactly
    what was copied)."""
    taken = records[:]
    del records[:len(taken)]
    return taken


def _pending_reaches() -> list[str]:
    """What the guards recorded, one sentence per door, every one drained."""
    problems = []
    targets = _drain(_CHANNEL_TARGETS)
    if targets:
        problems.append(
            f"opened a gRPC channel to {sorted(set(targets))}: stub the read, "
            "or install tests/_fake_firestore — a real client reaches "
            "production, or the outside"
        )
    hosts = _drain(_NETWORK_REACHES)
    if hosts:
        problems.append(
            f"reached the network: {sorted(set(hosts))}: stub the client or "
            "its transport — a real request leaves this machine"
        )
    sites = _drain(_CREDENTIAL_LOOKUPS)
    if sites:
        problems.append(
            "looked up credentials (google.auth.default) from "
            f"{sorted(set(sites))}: stub the client or its credentials — on "
            "a workstation that reads the developer's ADC and runs gcloud, "
            "on Cloud Build the build's own account"
        )
    return problems


# Every name the exit check reads is defined: a guarded module that fails to
# import stops this file with its own error, and nothing else.
_guard_the_channel_factories()
sys.addaudithook(_guarded_network)
google.auth.default = _guarded_default
google.auth._default.default = _guarded_default

# Only now, every guard live: the import that builds models.db.
with mock.patch("google.cloud.firestore.Client"):
    import models  # noqa: E402

assert isinstance(models.db, mock.MagicMock), type(models.db)


def _fail_on_reaches(item) -> None:
    problems = _pending_reaches()
    if problems:
        failure = pytest.fail.Exception("; ".join(problems))
        _FAILED_BY_THE_GUARD[item.nodeid] = failure
        raise failure


def pytest_collection_finish(session):
    # The test modules' top-level code ran during collection, and this
    # file's own import of models before it: no test owns what they recorded.
    _COLLECTION_REACHES.extend(_pending_reaches())


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    # Once the whole teardown is over: the test's own fixtures, and those of
    # a class, module or package that ends with it.
    try:
        result = yield
    except (KeyboardInterrupt, pytest.exit.Exception):
        raise  # the run stops: pytest_sessionfinish reports what remains
    except BaseException:
        _fail_on_reaches(item)  # chained to the teardown's own error
        raise
    _fail_on_reaches(item)
    return result


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    # Outermost: whatever the plugins inside made of this teardown, the
    # guard's failure is reported as a failure, with its message.
    report = yield
    failure = (_FAILED_BY_THE_GUARD.pop(item.nodeid, None)
               if call.when == "teardown" else None)
    if failure is None:
        return report
    if hasattr(report, "wasxfail"):  # the xfail plugin's expected failure
        del report.wasxfail
    if call.excinfo is None or call.excinfo.value is not failure:
        # The unittest plugin put a TestCase's leftover outcome in its place.
        if report.outcome != "failed":
            report.longrepr = str(failure)
        elif hasattr(report.longrepr, "addsection"):  # shown whatever
            report.longrepr.addsection(  # --show-capture says
                "Guard of tests/conftest.py", str(failure))
        else:
            report.longrepr = f"{report.longreprtext}\n\n{failure}"
    report.outcome = "failed"
    return report


@pytest.hookimpl(wrapper=True)
def pytest_sessionfinish(session):
    # Around pytest's own, which tears down what an interrupted run left
    # standing — and may raise while doing it, even pytest.exit asking for 0.
    try:
        result = yield
    except pytest.exit.Exception as stop:
        if _report_what_no_test_owns(session) and not stop.returncode:
            stop.returncode = pytest.ExitCode.TESTS_FAILED
        raise
    except BaseException:
        _report_what_no_test_owns(session)
        raise
    _report_what_no_test_owns(session)
    return result


def _report_what_no_test_owns(session) -> bool:
    problems = [f"during collection, {problem}"
                for problem in _drain(_COLLECTION_REACHES)]
    problems += [f"after the last test's check, {problem}"
                 for problem in _pending_reaches()]
    if not problems:
        return False
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")  # under -q the progress line is still open
        for problem in problems:
            reporter.write_line(problem, red=True)
    else:
        for problem in problems:
            print(problem, file=sys.stderr)
    if session.exitstatus in (pytest.ExitCode.OK,
                              pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    return True


def _is_production_handler(handler: logging.Handler) -> bool:
    """A handler ``utils.logging_setup.init_app`` built: it carries the
    RedactionFilter (matched by name — importing the module here would pull
    Flask into every collection)."""
    return any(
        type(f).__name__ == "RedactionFilter"
        and type(f).__module__.endswith("logging_setup")
        for f in handler.filters
    )


@pytest.fixture(autouse=True, scope="module")
def _no_module_leaves_the_production_log_chain():
    root = logging.getLogger()
    level = root.level
    yield
    for handler in list(root.handlers):
        if _is_production_handler(handler):
            root.removeHandler(handler)
    root.setLevel(level)
