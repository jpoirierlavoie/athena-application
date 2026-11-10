"""The suite's order independence, pinned where it once broke (finitions, E3).

A test module that installs the production log chain
(``utils.logging_setup.init_app``) left it on the root logger for every
later module, and ``tests/test_dav_root_discovery.py`` — whose 503 tests
assert that the traceback rides along (``record.exc_info``) — then failed
four tests, deterministically, whenever it ran AFTER one of those modules:
in a reversed full run, or in exactly the order below. The default
alphabetical order hid it. ``tests/conftest.py`` removes the chain at the
end of the module that installed it.

The check runs the failing order in a SUBPROCESS: within this process the
order is pytest's, and a leak can only be observed across module
boundaries. Fails without tests/conftest.py (7817412, 75efb45).

The same holds for production Firestore: run alone, or first, a module that
imported ``models`` unpatched gave the whole session a REAL client, which
reads production wherever ADC is set (tests/conftest.py, 2026-11-06). And for
credentials: a tracing test looked up the developer's on every run, through
``google.auth.default`` (tests/conftest.py, 2026-11-09); and for the network
at large. The guards' own behaviour is pinned here too: in a child pytest
for what spans tests, modules, the session or the process, in this process
for each door, the call site and the refusal.

A child pytest loads no plugin by itself (PYTEST_DISABLE_PLUGIN_AUTOLOAD)
and takes neither the developer's PYTEST_ADDOPTS nor colour, so that its
output, in file order, is the one these tests read.
"""

import _thread
import functools
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import threading
import types

import google.auth
import google.auth.exceptions
import pytest

from tests._real_app_probe import CHANNEL_FACTORIES

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CREDENTIALS = "looked up credentials (google.auth.default) from"
_CHANNEL = "opened a gRPC channel to"
_NETWORK = "reached the network:"
_AT_THE_END = ("during collection, ", "after the last test's check, ")


def _child_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if key not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return env


def _pytest_child(*args):
    """A child pytest from athena/, uncoloured."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--color=no", *args],
        cwd=_ATHENA, env=_child_env(), capture_output=True, text=True,
        timeout=240, stdin=subprocess.DEVNULL,
    )


def _run_planted(tmp_path, files: dict[str, str], *options):
    """Test files written outside the repository, run in the given order
    under this suite's conftest, loaded as a plugin."""
    for name, source in files.items():
        (tmp_path / name).write_text(source, encoding="utf-8")
    return _pytest_child("-p", "tests.conftest", *options,
                         *(str(tmp_path / name) for name in files))


def _names_site(text: str, opening: str, filename: str, line: int) -> bool:
    """The guard's sentence, naming a planted file's line — under a
    --basetemp inside athena/, its directory comes along."""
    return re.search(
        re.escape(opening) + r" \['(?:[^']*/)?"
        + re.escape(f"{filename}:{line}") + r"'\]", text) is not None


def _names_reach(text: str, opening: str, reached: str, filename: str,
                 line: int) -> bool:
    """A channel or network sentence: what was reached, and the planted
    file's line behind it."""
    return re.search(
        re.escape(f"{opening} ['{reached} (") + r"(?:[^']*/)?"
        + re.escape(f"{filename}:{line})'") + r"\]", text) is not None


def _session_lines(stdout: str) -> list[str]:
    """What the session's end reported, blaming no test."""
    return [line for line in stdout.splitlines()
            if line.startswith(_AT_THE_END)]


def test_a_module_that_installs_the_log_chain_leaks_it_to_no_later_module():
    run = _pytest_child(
        "tests/test_logging_setup.py",      # calls init_app directly
        "tests/test_portail_app.py",        # create_portail_app → init_app
        "tests/test_dav_root_discovery.py",  # asserts record.exc_info
    )
    tail = "\n".join((run.stdout or "").splitlines()[-15:])
    assert run.returncode == 0, tail
    assert "failed" not in tail, tail


def test_a_swallowed_google_api_channel_still_fails_the_test(tmp_path):
    # The readers that reach Firestore fail open: they catch the error. The
    # guard must fail the test anyway — loaded here as a plugin on a test
    # written outside the repository, that swallows the refusal.
    run = _run_planted(tmp_path, {"test_fail_open.py": (
        "from google.api_core import grpc_helpers\n"
        "\n"
        "def test_fail_open_reader():\n"
        "    try:\n"
        "        grpc_helpers.create_channel('firestore.googleapis.com:443')\n"
        "    except RuntimeError:\n"
        "        pass\n"
    )})
    assert run.returncode != 0, run.stdout
    assert _names_reach(run.stdout, _CHANNEL, "firestore.googleapis.com:443",
                        "test_fail_open.py", 5), run.stdout


def test_a_swallowed_network_reach_still_fails_the_test(tmp_path):
    # A REST client resolves its host, then connects: either step, swallowed
    # by a fail-open caller, fails the test. This machine stays reachable.
    run = _run_planted(tmp_path, {"test_network.py": (
        "import socket\n"
        "\n"
        "def test_dns_lookup():\n"
        "    try:\n"
        "        socket.getaddrinfo('guard.invalid', 443)\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "def test_connect():\n"
        "    with socket.socket() as sock:\n"
        "        sock.settimeout(1)\n"
        "        try:\n"
        "            sock.connect(('192.0.2.1', 9))\n"
        "        except Exception:\n"
        "            pass\n"
        "\n"
        "def test_this_machine():\n"
        "    socket.getaddrinfo('localhost', 80)\n"
        "    with socket.create_server(('127.0.0.1', 0)) as server:\n"
        "        socket.create_connection(server.getsockname(), 5).close()\n"
    )})
    assert run.returncode != 0, run.stdout
    assert "3 passed, 2 errors" in run.stdout, run.stdout
    assert "ERROR at teardown of test_dns_lookup" in run.stdout, run.stdout
    assert "ERROR at teardown of test_connect" in run.stdout, run.stdout
    assert "ERROR at teardown of test_this_machine" not in run.stdout, (
        run.stdout)
    assert _names_reach(run.stdout, _NETWORK, "guard.invalid",
                        "test_network.py", 5), run.stdout
    assert _names_reach(run.stdout, _NETWORK, "192.0.2.1",
                        "test_network.py", 13), run.stdout


def test_no_proxy_on_this_machine_hides_a_request(tmp_path):
    # A proxy on this machine — an environment variable, the Windows
    # registry — would take the request out, the guard seeing only a
    # connect to 127.0.0.1. The session runs with NO_PROXY=*: the client
    # connects directly, and the guard judges the real host.
    run = _run_planted(tmp_path, {"test_proxy.py": (
        "import requests\n"
        "\n"
        "def test_through_a_proxy(monkeypatch):\n"
        "    monkeypatch.setenv('HTTP_PROXY', 'http://127.0.0.1:9')\n"
        "    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:9')\n"
        "    try:\n"
        "        requests.get('http://guard.invalid/', timeout=2)\n"
        "    except Exception:\n"
        "        pass\n"
    )})
    assert run.returncode != 0, run.stdout
    assert "1 passed, 1 error" in run.stdout, run.stdout
    assert _names_reach(run.stdout, _NETWORK, "guard.invalid", "test_proxy.py",
                        7), run.stdout


def test_a_module_importing_models_unpatched_reaches_no_google_api():
    # test_budget imports models unpatched; test_mcp_output_schemas then ran
    # eleven tests' reads on whatever models.db was — production, in this
    # order, until conftest imported models under the patch first.
    run = _pytest_child("tests/test_budget.py",
                        "tests/test_mcp_output_schemas.py")
    tail = "\n".join((run.stdout or "").splitlines()[-15:])
    assert run.returncode == 0, tail
    assert "opened a gRPC channel" not in run.stdout, tail


def test_a_swallowed_credential_lookup_still_fails_the_test(tmp_path):
    # google.auth.default reads the developer's ADC and runs gcloud before any
    # channel exists, so the channel guard never sees it (a tracing test did
    # it on every run until 2026-11-09). The callers that hit it fail open
    # too: the test must fail anyway — under either name of the function,
    # and naming the line that called it. A test's own stub of the function
    # wins for that test, and is undone after it (file order: it runs first).
    source = (
        "import google.auth\n"
        "import google.auth._default\n"
        "\n"
        "def test_own_stub_wins(monkeypatch):\n"
        "    monkeypatch.setattr(google.auth, 'default',\n"
        "                        lambda *a, **k: ('creds', 'projet-temoin'))\n"
        "    assert google.auth.default() == ('creds', 'projet-temoin')\n"
        "\n"
        "def test_fail_open_lookup():\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "def test_private_home_lookup():\n"
        "    try:\n"
        "        google.auth._default.default()\n"
        "    except Exception:\n"
        "        pass\n"
    )
    run = _run_planted(tmp_path, {"test_credentials.py": source})
    lines = source.splitlines()
    public = lines.index("        google.auth.default()") + 1
    private = lines.index("        google.auth._default.default()") + 1
    assert run.returncode != 0, run.stdout
    # Every call passes; each swallowed lookup fails at teardown, the stubbed
    # one does not.
    assert "3 passed, 2 errors" in run.stdout, run.stdout
    assert "ERROR at teardown of test_fail_open_lookup" in run.stdout, (
        run.stdout)
    assert "ERROR at teardown of test_private_home_lookup" in run.stdout, (
        run.stdout)
    assert "test_own_stub_wins" not in run.stdout.split("short test summary")[-1]
    for line in (public, private):
        assert _names_site(run.stdout, _CREDENTIALS, "test_credentials.py",
                           line), run.stdout


def test_one_test_reaching_both_doors_fails_alone(tmp_path):
    # Each check reports both records and drains both: the second record
    # never fails the next test, which did nothing.
    run = _run_planted(tmp_path, {"test_both_doors.py": (
        "import google.auth\n"
        "from google.api_core import grpc_helpers\n"
        "\n"
        "def test_both_doors():\n"
        "    for reach in (\n"
        "        lambda: grpc_helpers.create_channel('firestore.googleapis.com:443'),\n"
        "        google.auth.default,\n"
        "    ):\n"
        "        try:\n"
        "            reach()\n"
        "        except Exception:\n"
        "            pass\n"
        "\n"
        "def test_innocent_next():\n"
        "    pass\n"
    )})
    assert run.returncode != 0, run.stdout
    assert "2 passed, 1 error" in run.stdout, run.stdout
    assert "ERROR at teardown of test_both_doors" in run.stdout, run.stdout
    assert "ERROR at teardown of test_innocent_next" not in run.stdout, (
        run.stdout)
    assert _names_reach(run.stdout, _CHANNEL, "firestore.googleapis.com:443",
                        "test_both_doors.py", 6), run.stdout
    assert _CREDENTIALS in run.stdout, run.stdout
    assert not _session_lines(run.stdout), run.stdout


def test_a_fixture_ending_with_a_test_counts_against_that_test(tmp_path):
    # A module fixture's teardown runs after its module's last test, before
    # the next module's first: what it records is charged to the former.
    run = _run_planted(tmp_path, {
        "test_a_module.py": (
            "import google.auth\n"
            "import pytest\n"
            "\n"
            "@pytest.fixture(autouse=True, scope='module')\n"
            "def lookup_as_it_ends():\n"
            "    yield\n"
            "    try:\n"
            "        google.auth.default()\n"
            "    except Exception:\n"
            "        pass\n"
            "\n"
            "def test_last_of_its_module():\n"
            "    pass\n"
        ),
        "test_b_module.py": "def test_innocent_next():\n    pass\n",
    })
    assert run.returncode != 0, run.stdout
    assert "2 passed, 1 error" in run.stdout, run.stdout
    assert "ERROR at teardown of test_last_of_its_module" in run.stdout, (
        run.stdout)
    assert "ERROR at teardown of test_innocent_next" not in run.stdout, (
        run.stdout)
    assert _names_site(run.stdout, _CREDENTIALS, "test_a_module.py", 8), (
        run.stdout)
    assert not _session_lines(run.stdout), run.stdout


@pytest.mark.parametrize("own_error, shown", [
    ("raise ValueError('teardown failed on its own')",
     "ValueError: teardown failed on its own"),
    ("pytest.fail('teardown failed on its own')",
     "Failed: teardown failed on its own"),
])
def test_a_failing_teardown_keeps_its_error_and_its_reach(tmp_path, own_error,
                                                          shown):
    # The guard's failure is chained to the fixture's own error, on the test
    # whose teardown made them — never left for the next test or for the
    # session's end; and a teardown that fails with nothing recorded keeps
    # its error, alone.
    run = _run_planted(tmp_path, {"test_failing_teardowns.py": (
        "import google.auth\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture\n"
        "def broken_teardown():\n"
        "    yield\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        f"    {own_error}\n"
        "\n"
        "@pytest.fixture\n"
        "def lone_teardown_error():\n"
        "    yield\n"
        "    raise ValueError('alone')\n"
        "\n"
        "def test_double_fault(broken_teardown):\n"
        "    pass\n"
        "\n"
        "def test_innocent_next():\n"
        "    pass\n"
        "\n"
        "def test_lone_error(lone_teardown_error):\n"
        "    pass\n"
    )})
    assert run.returncode != 0, run.stdout
    assert "3 passed, 2 errors" in run.stdout, run.stdout
    assert "ERROR at teardown of test_double_fault" in run.stdout, run.stdout
    assert "ERROR at teardown of test_lone_error" in run.stdout, run.stdout
    assert "ERROR at teardown of test_innocent_next" not in run.stdout, (
        run.stdout)
    assert shown in run.stdout, run.stdout
    assert "ValueError: alone" in run.stdout, run.stdout
    assert _names_site(run.stdout, _CREDENTIALS, "test_failing_teardowns.py",
                       8), run.stdout
    assert not _session_lines(run.stdout), run.stdout


@pytest.mark.parametrize("stop, returncode, banner", [
    ("pytest.exit('arret', returncode=0)", 1, "Exit: arret"),
    ("raise KeyboardInterrupt", 2, "KeyboardInterrupt"),
])
def test_a_stop_during_teardown_still_stops_the_run(tmp_path, stop,
                                                    returncode, banner):
    # The guard never turns an interruption into a mere teardown error: the
    # run stops there, and the reach is reported at its end.
    run = _run_planted(tmp_path, {"test_stop_in_teardown.py": (
        "import google.auth\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture\n"
        "def stops_as_it_ends():\n"
        "    yield\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        f"    {stop}\n"
        "\n"
        "def test_first(stops_as_it_ends):\n"
        "    pass\n"
        "\n"
        "def test_never_runs():\n"
        "    pass\n"
    )})
    assert run.returncode == returncode, run.stdout
    assert "1 passed in" in run.stdout, run.stdout  # test_never_runs did not
    assert banner in run.stdout, run.stdout
    reported = _session_lines(run.stdout)
    assert len(reported) == 1, run.stdout
    assert _names_site(reported[0], _AT_THE_END[1] + _CREDENTIALS,
                       "test_stop_in_teardown.py", 8), run.stdout


def test_an_xfail_mark_never_hides_a_reach(tmp_path):
    # The mark expects the test's own failure; pytest would turn the guard's
    # into an expected one too, and the run would pass. A teardown's own
    # error under the mark stays expected.
    run = _run_planted(tmp_path, {"test_known_bugs.py": (
        "import google.auth\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.xfail(reason='bogue connu')\n"
        "def test_known_bug():\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        "    assert False\n"
        "\n"
        "@pytest.fixture\n"
        "def failing_teardown():\n"
        "    yield\n"
        "    raise ValueError('expected too')\n"
        "\n"
        "@pytest.mark.xfail(reason='bogue connu')\n"
        "def test_other_bug(failing_teardown):\n"
        "    assert False\n"
    )})
    assert run.returncode != 0, run.stdout
    assert "3 xfailed, 1 error" in run.stdout, run.stdout
    assert "ERROR at teardown of test_known_bug" in run.stdout, run.stdout
    assert "ERROR at teardown of test_other_bug" not in run.stdout, run.stdout
    assert _names_site(run.stdout, _CREDENTIALS, "test_known_bugs.py", 7), (
        run.stdout)
    assert not _session_lines(run.stdout), run.stdout


def test_a_unittest_leftover_outcome_never_replaces_the_guards_failure(
        tmp_path):
    # A TestCase runs its body and its tearDown in the call phase; a second
    # outcome is put in the teardown's place by pytest's unittest plugin —
    # a skip would pass the run, an error would hide the guard's message,
    # which must show even with captured output hidden.
    run = _run_planted(tmp_path, {"test_cases.py": (
        "import unittest\n"
        "\n"
        "import google.auth\n"
        "\n"
        "\n"
        "class SkipsTwice(unittest.TestCase):\n"
        "    def tearDown(self):\n"
        "        self.skipTest('teardown skip')\n"
        "\n"
        "    def test_reach_then_skip(self):\n"
        "        try:\n"
        "            google.auth.default()\n"
        "        except Exception:\n"
        "            pass\n"
        "        self.skipTest('body skip')\n"
        "\n"
        "\n"
        "class FailsTwice(unittest.TestCase):\n"
        "    def tearDown(self):\n"
        "        raise RuntimeError('tearDown error')\n"
        "\n"
        "    def test_reach_then_fail(self):\n"
        "        try:\n"
        "            google.auth.default()\n"
        "        except Exception:\n"
        "            pass\n"
        "        self.fail('body failure')\n"
    )}, "--show-capture=no")
    assert run.returncode != 0, run.stdout
    assert "1 failed, 1 skipped, 2 errors" in run.stdout, run.stdout
    assert "RuntimeError: tearDown error" in run.stdout, run.stdout
    for line in (12, 24):
        assert _names_site(run.stdout, _CREDENTIALS, "test_cases.py", line), (
            run.stdout)


def test_a_reach_during_collection_fails_the_session_not_a_test(tmp_path):
    # A test module's top-level code runs during collection, before any
    # test: the session fails, on a line of its own, and no test is blamed.
    run = _run_planted(tmp_path, {"test_at_import.py": (
        "import google.auth\n"
        "try:\n"
        "    google.auth.default()\n"
        "except Exception:\n"
        "    pass\n"
        "\n"
        "def test_innocent():\n"
        "    pass\n"
    )})
    assert run.returncode == 1, run.stdout
    assert "1 passed in" in run.stdout, run.stdout
    assert "error" not in run.stdout.splitlines()[-1], run.stdout
    reported = _session_lines(run.stdout)
    assert len(reported) == 1, run.stdout
    assert _names_site(reported[0], _AT_THE_END[0] + _CREDENTIALS,
                       "test_at_import.py", 3), run.stdout


def test_what_an_interrupted_run_tears_down_is_still_reported(tmp_path):
    # pytest.exit skips the test's own teardown; pytest tears its fixtures
    # down at the end of the session, before the final check.
    run = _run_planted(tmp_path, {"test_interrupted.py": (
        "import google.auth\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def lookup_as_it_ends():\n"
        "    yield\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "def test_stops_the_run(lookup_as_it_ends):\n"
        "    pytest.exit('arret', returncode=0)\n"
    )})
    assert run.returncode == 1, run.stdout
    reported = _session_lines(run.stdout)
    assert len(reported) == 1, run.stdout
    assert _names_site(reported[0], _AT_THE_END[1] + _CREDENTIALS,
                       "test_interrupted.py", 8), run.stdout


def test_a_crash_tearing_down_an_interrupted_run_still_reports(tmp_path):
    # pytest's own end-of-session teardown raises: the final check still
    # runs around it.
    run = _run_planted(tmp_path, {"test_crash_at_the_end.py": (
        "import google.auth\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def crashes_as_it_ends():\n"
        "    yield\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        "    raise ValueError('session finalizer fails')\n"
        "\n"
        "def test_stops_the_run(crashes_as_it_ends):\n"
        "    pytest.exit('arret', returncode=0)\n"
    )})
    assert run.returncode != 0, run.stdout
    reported = _session_lines(run.stdout)
    assert len(reported) == 1, run.stdout + run.stderr
    assert _names_site(reported[0], _AT_THE_END[1] + _CREDENTIALS,
                       "test_crash_at_the_end.py", 8), run.stdout


def test_an_exit_asking_for_0_at_the_session_end_keeps_the_failure(tmp_path):
    # A finalizer of the interrupted run reaches, then calls pytest.exit
    # with status 0 while pytest tears it down: the reach still fails it.
    run = _run_planted(tmp_path, {"test_exit_at_the_end.py": (
        "import google.auth\n"
        "import pytest\n"
        "\n"
        "@pytest.fixture(scope='session')\n"
        "def exits_as_it_ends():\n"
        "    yield\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        "    pytest.exit('fin', returncode=0)\n"
        "\n"
        "def test_stops_the_run(exits_as_it_ends):\n"
        "    pytest.exit('arret', returncode=0)\n"
    )})
    assert run.returncode == 1, run.stdout + run.stderr
    reported = _session_lines(run.stdout)
    assert len(reported) == 1, run.stdout
    assert _names_site(reported[0], _AT_THE_END[1] + _CREDENTIALS,
                       "test_exit_at_the_end.py", 8), run.stdout


@pytest.mark.parametrize("module", ["models", "grpc.experimental.aio"])
def test_a_failed_import_leaves_no_error_at_exit(module):
    # conftest stops at its import of models, or of a guarded factory's
    # module — a gRPC release that drops it; the exit check, registered
    # earlier, still finds every name it reads.
    script = (
        "import sys\n"
        f"sys.modules[{module!r}] = None\n"
        "import tests.conftest\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", script], cwd=_ATHENA, env=_child_env(),
        capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    assert f"import of {module} halted" in run.stderr, run.stderr  # planted
    assert "atexit" not in run.stderr, run.stderr
    assert "NameError" not in run.stderr, run.stderr


def test_the_session_runs_without_any_proxy_variable():
    # Whatever the developer's environment holds: gRPC ignores NO_PROXY=*,
    # so the variables it reads are gone; requests and urllib keep
    # NO_PROXY=*, against the Windows registry's proxy.
    script = (
        "import os\n"
        "for name in ('grpc_proxy', 'HTTPS_PROXY', 'http_proxy', 'ALL_PROXY'):\n"
        "    os.environ[name] = 'http://127.0.0.1:9'\n"
        "import tests.conftest\n"
        "print('@@PROXIES@@', sorted({k.lower() for k in os.environ\n"
        "                             if k.lower().endswith('proxy')}),\n"
        "      os.environ.get('NO_PROXY'))\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", script], cwd=_ATHENA, env=_child_env(),
        capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "@@PROXIES@@ ['no_proxy'] *" in run.stdout, run.stdout


def test_a_reach_after_the_session_fails_the_process(tmp_path):
    # An atexit handler a test registered runs after pytest is done: the
    # process exits in error, with the line on stderr.
    run = _run_planted(tmp_path, {"test_after_the_session.py": (
        "import atexit\n"
        "\n"
        "import google.auth\n"
        "\n"
        "\n"
        "def _late():\n"
        "    try:\n"
        "        google.auth.default()\n"
        "    except Exception:\n"
        "        pass\n"
        "\n"
        "\n"
        "def test_leaves_work_behind():\n"
        "    atexit.register(_late)\n"
    )})
    assert run.returncode == 1, run.stdout + run.stderr
    assert "1 passed in" in run.stdout, run.stdout
    assert _names_site(run.stderr, "at exit, " + _CREDENTIALS,
                       "test_after_the_session.py", 8), run.stderr


_DOORS = ("google.auth.default", "google.auth._default.default",
          *(f"{home}.{name}" for home, name in CHANNEL_FACTORIES))


def test_the_guards_are_live_before_conftest_imports_models(tmp_path):
    # conftest imports models — the app's own import chain — under the
    # Firestore patch: a lookup, a channel or the network there must meet
    # the guards. An audit hook in the child reads every door as that import
    # begins, and tries a DNS lookup of a name that resolves nowhere — which
    # the guard refuses, records, and reports as collection's.
    (tmp_path / "test_nothing.py").write_text(
        "def test_nothing():\n    pass\n", encoding="utf-8")
    script = (
        "import importlib, json, socket, sys\n"
        "doors = [name.rsplit('.', 1) for name in json.loads(sys.argv[2])]\n"
        "homes = {home: importlib.import_module(home) for home, _ in doors}\n"
        "seen = []\n"
        "def hook(event, args):\n"
        "    if event == 'import' and args[0] == 'models' and not seen:\n"
        "        try:\n"
        "            socket.getaddrinfo('guard.invalid', 443)\n"
        "            network = 'open'\n"
        "        except RuntimeError:\n"
        "            network = 'refused'\n"
        "        except OSError:\n"
        "            network = 'open'\n"
        "        seen.append([getattr(homes[home], name).__module__\n"
        "                     for home, name in doors] + [network])\n"
        "sys.addaudithook(hook)\n"
        "import pytest\n"
        "rc = pytest.main(['-q', '-p', 'no:cacheprovider', '--color=no',\n"
        "                  '-p', 'tests.conftest', sys.argv[1]])\n"
        "print('@@AT-MODELS-IMPORT@@', json.dumps(seen))\n"
        "sys.exit(int(rc))\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "test_nothing.py"),
         json.dumps(_DOORS)],
        cwd=_ATHENA, env=_child_env(), capture_output=True, text=True,
        timeout=240, stdin=subprocess.DEVNULL,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    seen = [line.split(" ", 1)[1] for line in run.stdout.splitlines()
            if line.startswith("@@AT-MODELS-IMPORT@@ ")]
    assert seen, run.stdout
    assert json.loads(seen[-1]) == [
        ["tests.conftest"] * len(_DOORS) + ["refused"]], run.stdout
    reported = _session_lines(run.stdout)
    assert len(reported) == 1, run.stdout
    assert reported[0].startswith(
        f"{_AT_THE_END[0]}{_NETWORK} ['guard.invalid ("), run.stdout


def test_the_running_environment_is_set_apart_only_inside_athena():
    # Computed when conftest is imported, from the running interpreter's
    # prefixes — here a child whose sys.prefix says athena/.venv, and whose
    # exec prefix is the base interpreter's, outside athena/ whatever the
    # environment running this suite.
    script = (
        "import os, sys\n"
        "sys.prefix = os.path.join(os.getcwd(), '.venv')\n"
        "sys.exec_prefix = sys.base_exec_prefix\n"
        "import tests.conftest as guard\n"
        "print('@@ENVS@@', guard._ENVS_IN_ATHENA == [os.path.abspath(sys.prefix)])\n"
    )
    run = subprocess.run(
        [sys.executable, "-c", script], cwd=_ATHENA, env=_child_env(),
        capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "@@ENVS@@ True" in run.stdout, run.stdout + run.stderr
    from tests import conftest

    outside = os.path.join(os.path.dirname(_ATHENA), "venv-ailleurs")
    inside = os.path.join(_ATHENA, ".venv")
    assert conftest._envs_in(_ATHENA, (inside, outside, inside)) == [
        os.path.abspath(inside)]


# In this process: the guard on a real stack. Each test drains what it made
# the guard record, so that its own teardown passes.

def _calls_the_default():
    return google.auth.default()


def _calls(callee):
    return callee()


def _filed_under(function, *parts):
    """The same function, its code filed under athena/<parts>."""
    code = function.__code__.replace(
        co_filename=os.path.join(_ATHENA, *parts))
    return types.FunctionType(code, function.__globals__, function.__name__)


def test_the_guard_refuses_and_names_the_app_line_behind_a_library():
    from tests import conftest

    library = _filed_under(_calls_the_default, ".venv", "Lib",
                           "site-packages", "bibliotheque", "__init__.py")
    app = _filed_under(_calls, "utils", "_site_plante.py")
    with pytest.raises(google.auth.exceptions.DefaultCredentialsError):
        app(library)
    line = _calls.__code__.co_firstlineno + 1
    problems = conftest._pending_reaches()
    assert len(problems) == 1, problems
    assert problems[0].startswith(
        f"{_CREDENTIALS} ['utils/_site_plante.py:{line}']"), problems


def test_a_virtualenv_inside_athena_is_not_this_repository(monkeypatch):
    # A developer's athena/.venv: its launcher is a file under athena/, yet
    # no line of this repository.
    from tests import conftest

    monkeypatch.setattr(conftest, "_ENVS_IN_ATHENA",
                        [os.path.join(_ATHENA, ".venv")])
    launcher = _filed_under(_calls, ".venv", "Scripts", "pytest.exe",
                            "__main__.py")
    app = _filed_under(_calls, "utils", "_site_plante.py")
    with pytest.raises(google.auth.exceptions.DefaultCredentialsError):
        app(functools.partial(launcher, google.auth.default))
    line = _calls.__code__.co_firstlineno + 1
    problems = conftest._pending_reaches()
    assert len(problems) == 1, problems
    assert problems[0].startswith(
        f"{_CREDENTIALS} ['utils/_site_plante.py:{line}']"), problems


def test_a_lookup_with_no_python_caller_is_refused_and_recorded(monkeypatch):
    # A thread started on the guard itself: no Python frame below it.
    from tests import conftest

    raised = []
    done = threading.Event()

    def unraisable(failure):
        raised.append(failure.exc_type)
        done.set()

    monkeypatch.setattr(sys, "unraisablehook", unraisable)
    _thread.start_new_thread(google.auth.default, ())
    assert done.wait(30)
    assert raised == [google.auth.exceptions.DefaultCredentialsError]
    problems = conftest._pending_reaches()
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{_CREDENTIALS} ['?']"), problems


def test_a_record_added_while_a_list_drains_waits_for_the_next_check():
    from tests import conftest

    class RecordedMeanwhile(list):
        """A thread's append, landing as the list is copied — by a slice or
        by iteration."""

        appended = False

        def _meanwhile(self):
            if not self.appended:
                self.appended = True
                self.append("utils/fil.py:9")

        def __getitem__(self, index):
            taken = super().__getitem__(index)
            if isinstance(index, slice):
                self._meanwhile()
            return taken

        def __iter__(self):
            taken = list(super().__iter__())
            self._meanwhile()
            return iter(taken)

    records = RecordedMeanwhile(["tests/test_x.py:1"])
    assert conftest._drain(records) == ["tests/test_x.py:1"]
    assert records == ["utils/fil.py:9"]


def _resolves(host):
    return socket.getaddrinfo(host, 443)


def _connects(address):
    with socket.socket() as sock:
        sock.settimeout(1)
        sock.connect(address)


def _opens(factory, target):
    return factory(target, None)


@pytest.mark.parametrize("reach, call, reached", [
    ("dns", _resolves, "guard.invalid"),
    ("connect", _connects, "192.0.2.1"),
])
def test_a_network_reach_is_refused_with_its_line(reach, call, reached):
    # A RuntimeError, which no retry loop waits on, before any packet leaves.
    from tests import conftest

    with pytest.raises(RuntimeError):
        call("guard.invalid" if reach == "dns" else ("192.0.2.1", 9))
    line = call.__code__.co_firstlineno + (1 if reach == "dns" else 3)
    problems = conftest._pending_reaches()
    assert len(problems) == 1, problems
    assert _names_reach(problems[0], _NETWORK, reached,
                        "test_suite_isolation.py", line), problems


@pytest.mark.parametrize("home, name", CHANNEL_FACTORIES)
def test_every_channel_factory_refuses_a_target_out_here_too(home, name):
    from tests import conftest

    factory = getattr(importlib.import_module(home), name)
    with pytest.raises(RuntimeError):
        _opens(factory, "example.invalid:443")
    line = _opens.__code__.co_firstlineno + 1
    problems = conftest._pending_reaches()
    assert len(problems) == 1, problems
    assert _names_reach(problems[0], _CHANNEL, "example.invalid:443",
                        "test_suite_isolation.py", line), problems


def test_this_machine_stays_reachable_here_too():
    from tests import conftest

    socket.getaddrinfo("localhost", 80)
    with socket.create_server(("127.0.0.1", 0)) as server:
        socket.create_connection(server.getsockname(), 5).close()
    insecure = getattr(importlib.import_module("grpc"), "insecure_channel")
    insecure("127.0.0.1:1").close()
    # Where a cruder rule than tests/_real_app_probe's would refuse.
    socket.getaddrinfo("::ffff:127.0.0.1", 80)
    socket.getaddrinfo("0:0:0:0:0:0:0:1", 80)
    insecure("[::ffff:127.0.0.1]:1").close()
    assert conftest._pending_reaches() == []


def _sends(address):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(b"x", address)


def _sends_a_message(address):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendmsg([b"x"], [], 0, address)


_NO_SENDMSG = pytest.mark.skipif(not hasattr(socket.socket, "sendmsg"),
                                 reason="no sendmsg on this platform")


@pytest.mark.parametrize("call, argument, reached", [
    # A naive « starts with 127. » rule lets this name out.
    pytest.param(socket.getaddrinfo, ("127.guard.invalid", 443),
                 "127.guard.invalid", id="127-name"),
    pytest.param(socket.getaddrinfo, ("2001:db8::1", 443), "2001:db8::1",
                 id="ipv6"),
    pytest.param(socket.gethostbyname, ("guard.invalid",), "guard.invalid",
                 id="gethostbyname"),
    pytest.param(socket.gethostbyaddr, ("192.0.2.1",), "192.0.2.1",
                 id="gethostbyaddr"),
    pytest.param(socket.getnameinfo, (("192.0.2.1", 80), 0), "192.0.2.1",
                 id="getnameinfo"),
    pytest.param(_sends, (("192.0.2.1", 9),), "192.0.2.1", id="sendto"),
    pytest.param(_sends_a_message, (("192.0.2.1", 9),), "192.0.2.1",
                 id="sendmsg", marks=_NO_SENDMSG),
])
def test_every_network_event_is_refused_here_too(call, argument, reached):
    from tests import conftest

    with pytest.raises(RuntimeError):
        call(*argument)
    problems = conftest._pending_reaches()
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{_NETWORK} ['{reached} ("), problems


def test_a_channel_to_a_127_name_is_refused_here_too():
    from tests import conftest

    insecure = getattr(importlib.import_module("grpc"), "insecure_channel")
    with pytest.raises(RuntimeError):
        _opens(insecure, "127.guard.invalid:443")
    problems = conftest._pending_reaches()
    assert len(problems) == 1, problems
    assert problems[0].startswith(f"{_CHANNEL} ['127.guard.invalid:443 ("), (
        problems)


def test_whose_code_a_frame_is():
    # The call site prefers this repository's line, then a line no library
    # holds; the interpreter's own files — a virtualenv's launcher too — are
    # a library's, wherever they live.
    from tests import conftest

    assert conftest._place(os.path.join(_ATHENA, "utils", "x.py")) == (
        "utils/x.py", "ours")
    assert conftest._place(os.__file__)[1] == "library"
    assert conftest._place(os.path.join(
        sys.prefix, "Scripts", "pytest.exe", "__main__.py"))[1] == "library"
    assert conftest._place(os.path.join(
        sys.base_prefix, "bin", "pytest"))[1] == "library"
    assert conftest._place(os.path.join(
        os.sep, "usr", "lib", "python3", "dist-packages", "requests",
        "api.py")) == ("requests/api.py", "library")
    # Outside athena/ whatever --basetemp says.
    assert conftest._place(os.path.join(
        os.path.dirname(_ATHENA), "ailleurs", "test_planted.py")) == (
        "test_planted.py", "other")
    assert conftest._place("<frozen runpy>") == ("<frozen runpy>", "library")
