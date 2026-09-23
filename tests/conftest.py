"""Shared fixtures.

Two things make this app awkward to test, and both are handled here so the
tests themselves stay readable:

1. `main.py` reads its configuration into module-level constants at import
   time (`INGESTION_TOKEN`, `ROSTER_PASSWORD`, `DB_FILE`, ...). A test that
   needs a different configuration therefore needs a fresh import, not just a
   different environment variable.
2. It calls `load_dotenv()` at import, which would pull in whatever is in the
   developer's own `.env`. That makes results depend on the machine — and in
   particular a test that deletes a variable to check the fail-closed path
   would silently get it back. `load_dotenv` is stubbed out so every test
   states its own configuration in full.
"""
import importlib
import sys
import uuid
from pathlib import Path

import dotenv
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# What every test starts from unless it says otherwise.
BASELINE = {
    "ROSTER_USERNAME": "admin",
    "ROSTER_PASSWORD": "testpass",
    "STALE_AFTER_MINUTES": "60",
}
# Optional settings, cleared unless a test asks for them.
OPTIONAL = ("INGESTION_TOKEN", "DOWNLOAD_PASSPHRASE", "PUBLIC_URL")

ROSTER_AUTH = ("admin", "testpass")


@pytest.fixture
def build_server(tmp_path, monkeypatch):
    """Factory returning (module, client) for a freshly configured server.

    Pass `KEY=None` to unset a variable. Each call gets its own SQLite file,
    so tests never see each other's data.
    """
    clients = []

    def _build(**overrides):
        monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_k: False)
        monkeypatch.setenv("DB_FILE", str(tmp_path / f"{uuid.uuid4().hex}.db"))
        for key, value in BASELINE.items():
            monkeypatch.setenv(key, value)
        for key in OPTIONAL:
            monkeypatch.delenv(key, raising=False)
        for key, value in overrides.items():
            if value is None:
                monkeypatch.delenv(key, raising=False)
            else:
                monkeypatch.setenv(key, str(value))

        module = (importlib.reload(sys.modules["main"]) if "main" in sys.modules
                  else importlib.import_module("main"))
        client = TestClient(module.app)
        client.__enter__()          # runs the startup event: schema + migrations
        clients.append(client)
        return module, client

    yield _build

    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def server(build_server):
    """The common case: default configuration, no token, no passphrase."""
    _module, client = build_server()
    return client


def post_event(client, data, token=None, timestamp="2026-09-23T12:00:00"):
    """Send one addon payload the way a watcher would."""
    headers = {"X-Ingestion-Token": token} if token else {}
    return client.post("/api/log-update",
                       json={"timestamp": timestamp, "data": data},
                       headers=headers)


def accepted(client, data, **kwargs):
    """Post a payload and assert the server took it."""
    body = post_event(client, data, **kwargs).json()
    assert body["status"] == "success", f"expected {data!r} to be accepted, got {body}"
    return body


def rejected(client, data, **kwargs):
    """Post a payload and assert the server refused it, returning the reason.

    Note the endpoint answers 200 with a status field rather than an HTTP
    error, so a watcher can tell "this one payload is bad" apart from "the
    server is unreachable" — see savedvars_watcher.ServerRejected.
    """
    response = post_event(client, data, **kwargs)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error", f"expected {data!r} to be rejected, got {body}"
    return body["detail"]


def leaderboard(client):
    """Current player states keyed by name."""
    return {p["name"]: p for p in client.get("/api/leaderboard").json()}
