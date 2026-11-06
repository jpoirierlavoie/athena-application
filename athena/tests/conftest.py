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
same patch 179 modules use: every selection starts where the full run does.
And as a backstop, every Google gRPC client (Firestore, Secret Manager,
Cloud Tasks, Cloud Logging) opens its channel through
``google.api_core.grpc_helpers.create_channel`` on its first RPC: a
``*.googleapis.com`` target is refused there AND recorded, because the
readers that hit it fail open — they swallow the error — so only the
teardown check below fails the test.
"""

import logging
import os
import sys
from unittest import mock

import pytest
from google.api_core import grpc_helpers

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

with mock.patch("google.cloud.firestore.Client"):
    import models  # noqa: E402,F401  — models.db is a MagicMock from here on

_GOOGLE_API_TARGETS: list[str] = []
_create_channel = grpc_helpers.create_channel


def _guarded_create_channel(target, *args, **kwargs):
    if "googleapis.com" in str(target):
        _GOOGLE_API_TARGETS.append(str(target))
        raise RuntimeError(f"tests never reach Google APIs ({target})")
    return _create_channel(target, *args, **kwargs)


grpc_helpers.create_channel = _guarded_create_channel


@pytest.fixture(autouse=True)
def _no_test_reaches_a_google_api():
    yield
    if _GOOGLE_API_TARGETS:
        targets = sorted(set(_GOOGLE_API_TARGETS))
        _GOOGLE_API_TARGETS.clear()
        pytest.fail(
            f"opened a gRPC channel to {targets}: stub the read, or install "
            "tests/_fake_firestore — a real client reaches production"
        )


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
