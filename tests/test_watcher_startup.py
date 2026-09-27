"""The watcher starts with Windows by default -- once, and only until the
player says otherwise -- and waits long enough not to re-send duplicates.

The registry is never touched here: set_startup is replaced, so these tests
only check the decisions, not Windows.
"""
import sys

import pytest

import savedvars_watcher as watcher


@pytest.fixture
def packaged(tmp_path, monkeypatch):
    """Looks like the frozen .exe on Windows, with the registry faked."""
    calls = []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(watcher.os, "name", "nt")
    monkeypatch.setattr(watcher, "STARTUP_DECIDED_PATH", str(tmp_path / ".savedvars_watcher_startup"))
    monkeypatch.setattr(watcher, "set_startup", lambda enabled: calls.append(enabled) or True)
    return calls


def test_the_first_run_turns_start_with_windows_on(packaged):
    assert watcher.apply_startup_default() is True
    assert packaged == [True]


def test_it_only_happens_once(packaged):
    """Otherwise a player who turned it off would be switched back on at
    every launch -- which it would never stop doing."""
    watcher.apply_startup_default()
    assert watcher.apply_startup_default() is False
    assert packaged == [True]


def test_turning_it_off_first_is_respected(packaged):
    """The tray toggle records the choice, so the default never runs over it."""
    watcher.mark_startup_decided()
    assert watcher.apply_startup_default() is False
    assert packaged == []


def test_running_from_source_never_touches_startup(packaged, monkeypatch):
    """A dev run would register the Python interpreter, not the watcher."""
    monkeypatch.delattr(sys, "frozen")
    assert watcher.apply_startup_default() is False
    assert packaged == []


def test_the_watcher_waits_long_enough_to_avoid_duplicates():
    """Load testing the real server: 50 players syncing at once reached 2.4 s.
    Giving up sooner re-sends an event the server may already have saved."""
    captured = {}

    class Ok:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "success"}

    class Session:
        def post(self, *_args, timeout, **_kwargs):
            captured["timeout"] = timeout
            return Ok()

    original = watcher.SESSION
    watcher.SESSION = Session()
    try:
        watcher.send_packet("http://unused", "Ayla,ZONE,1790000000,Ironforge")
    finally:
        watcher.SESSION = original
    assert captured["timeout"] >= 10
