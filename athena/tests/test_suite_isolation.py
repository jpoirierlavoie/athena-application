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
reads production wherever ADC is set (tests/conftest.py, 2026-11-06).
"""

import os
import subprocess
import sys

_ATHENA = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_a_module_that_installs_the_log_chain_leaks_it_to_no_later_module():
    order = [
        "tests/test_logging_setup.py",      # calls init_app directly
        "tests/test_portail_app.py",        # create_portail_app → init_app
        "tests/test_dav_root_discovery.py",  # asserts record.exc_info
    ]
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", *order],
        cwd=_ATHENA, capture_output=True, text=True, timeout=240,
        stdin=subprocess.DEVNULL,
    )
    tail = "\n".join((run.stdout or "").splitlines()[-15:])
    assert run.returncode == 0, tail
    assert "failed" not in tail, tail


def test_a_swallowed_google_api_channel_still_fails_the_test(tmp_path):
    # The readers that reach Firestore fail open: they catch the error. The
    # guard must fail the test anyway — loaded here as a plugin on a test
    # written outside the repository, that swallows the refusal.
    (tmp_path / "test_fail_open.py").write_text(
        "from google.api_core import grpc_helpers\n"
        "\n"
        "def test_fail_open_reader():\n"
        "    try:\n"
        "        grpc_helpers.create_channel('firestore.googleapis.com:443')\n"
        "    except RuntimeError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "tests.conftest", str(tmp_path / "test_fail_open.py")],
        cwd=_ATHENA, capture_output=True, text=True, timeout=240,
        stdin=subprocess.DEVNULL,
    )
    assert run.returncode != 0, run.stdout
    assert "opened a gRPC channel to ['firestore.googleapis.com:443']" in (
        run.stdout
    ), run.stdout


def test_a_module_importing_models_unpatched_reaches_no_google_api():
    # test_budget imports models unpatched; test_mcp_output_schemas then ran
    # eleven tests' reads on whatever models.db was — production, in this
    # order, until conftest imported models under the patch first.
    order = ["tests/test_budget.py", "tests/test_mcp_output_schemas.py"]
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-p", "no:randomly", *order],
        cwd=_ATHENA, capture_output=True, text=True, timeout=240,
        stdin=subprocess.DEVNULL,
    )
    tail = "\n".join((run.stdout or "").splitlines()[-15:])
    assert run.returncode == 0, tail
    assert "opened a gRPC channel" not in run.stdout, tail
