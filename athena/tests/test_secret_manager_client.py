"""One Secret Manager client per process (2026-11-12), main service and portal.

Each ``SecretManagerServiceClient`` opens its own gRPC channel and resolves
credentials. ``config.py`` and ``client/config.py`` built one PER SECRET —
five per gunicorn worker on the main service, at import, inside the start a
cold request waits for. They now share one; a client that fails to build is
not cached, so the next secret tries again, as each did before.
"""

import importlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("FIREBASE_PROJECT_ID", "test-project")
os.environ.setdefault("FIREBASE_STORAGE_BUCKET", "test-bucket")
os.environ.setdefault("AUTHORIZED_USER_EMAIL", "test@example.com")

from google.cloud import secretmanager  # noqa: E402

MODULES = ["config", "client.config"]


@pytest.fixture
def clients(monkeypatch):
    """Every client built, in order; ``fail_first`` makes the first raise."""
    built = []
    state = {"fail_first": False}

    class _Client:
        def __init__(self):
            built.append(self)
            if state["fail_first"] and len(built) == 1:
                raise RuntimeError("no credentials yet")

        def access_secret_version(self, request):
            return SimpleNamespace(payload=SimpleNamespace(
                data=request["name"].encode("utf-8")))

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", _Client)
    return built, state


def _fresh(name):
    module = importlib.import_module(name)
    module._secret_manager_client.cache_clear()
    module._from_secret_manager.cache_clear()
    return module


def _path(secret_id: str) -> str:
    project = os.environ["FIREBASE_PROJECT_ID"]
    return f"projects/{project}/secrets/{secret_id}/versions/latest"


@pytest.mark.parametrize("name", MODULES)
def test_every_secret_is_read_through_one_client(clients, name):
    built, _state = clients
    module = _fresh(name)
    try:
        values = [module._from_secret_manager(s) for s in ("a", "b", "c", "a")]
    finally:
        _fresh(name)
    assert values == [_path(s) for s in ("a", "b", "c", "a")]
    assert len(built) == 1


@pytest.mark.parametrize("name", MODULES)
def test_a_client_that_failed_to_build_is_built_again(clients, name):
    built, state = clients
    state["fail_first"] = True
    module = _fresh(name)
    try:
        with pytest.raises(RuntimeError):
            module._from_secret_manager("a")
        assert module._from_secret_manager("b") == _path("b")
    finally:
        _fresh(name)
    assert len(built) == 2


def test_prefetch_secrets_warms_all_startup_secrets_through_one_client(clients, monkeypatch):
    built, _state = clients
    module = _fresh("config")
    monkeypatch.setenv("ENV", "production")
    try:
        module._prefetch_secrets()
        values = {sid: module._from_secret_manager(sid) for sid in module._STARTUP_SECRET_IDS}
    finally:
        _fresh("config")
    assert len(built) == 1
    assert values == {sid: _path(sid) for sid in module._STARTUP_SECRET_IDS}


def test_cf_origin_secret_fails_closed_in_production_and_stays_optional_in_dev(
    clients, monkeypatch
):
    module = _fresh("config")
    monkeypatch.setenv("ENV", "development")
    monkeypatch.delenv("CF_ORIGIN_SECRET", raising=False)
    assert (
        module._secret(
            "cf-origin-secret",
            "CF_ORIGIN_SECRET",
            required=module._is_production(),
        )
        == ""
    )

    monkeypatch.setenv("ENV", "production")
    monkeypatch.setattr(module, "_from_secret_manager", lambda _sid: "")
    with pytest.raises(RuntimeError, match="Empty required secret"):
        module._secret(
            "cf-origin-secret",
            "CF_ORIGIN_SECRET",
            required=module._is_production(),
        )

    def _boom(_sid):
        raise RuntimeError("secret manager unavailable")

    monkeypatch.setattr(module, "_from_secret_manager", _boom)
    with pytest.raises(RuntimeError, match="secret manager unavailable"):
        module._secret(
            "cf-origin-secret",
            "CF_ORIGIN_SECRET",
            required=module._is_production(),
        )


def test_portail_prefetch_secrets_warms_all_startup_secrets_through_one_client(
    clients, monkeypatch
):
    built, _state = clients
    module = _fresh("client.config")
    monkeypatch.setenv("ENV", "production")
    try:
        module._prefetch_secrets()
        values = {
            sid: module._from_secret_manager(sid)
            for sid in module._PORTAIL_STARTUP_SECRET_IDS
        }
    finally:
        _fresh("client.config")
    assert len(built) == 1
    assert values == {
        sid: _path(sid) for sid in module._PORTAIL_STARTUP_SECRET_IDS
    }


def test_portail_prefetch_secrets_is_noop_outside_production(clients, monkeypatch):
    built, _state = clients
    module = _fresh("client.config")
    monkeypatch.setenv("ENV", "development")
    try:
        module._prefetch_secrets()
    finally:
        _fresh("client.config")
    assert built == []
