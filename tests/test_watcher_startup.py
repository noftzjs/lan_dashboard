"""The watcher starts with Windows by default -- once, and only until the
player says otherwise -- and waits long enough not to re-send duplicates.

The registry is never touched here: set_startup is replaced, so these tests
only check the decisions, not Windows.
"""
import os
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


# --- a newer download replaces an older watcher that is still running ------------
# An old watcher that auto-starts with Windows holds the single-instance lock,
# so the new download a player just double-clicked would be refused and the
# old one would carry on. A watcher from a *different folder* is replaced; the
# same exe started twice is still a double-click and still refused.

OWN = r"C:\Users\me\Desktop\LanDashboardWatcher\savedvars_watcher.exe"


def test_a_watcher_from_another_folder_is_replaced():
    running = [(100, r"C:\Users\me\Downloads\LanDashboardWatcher\savedvars_watcher.exe"),
               (101, r"C:\Users\me\Downloads\LanDashboardWatcher\savedvars_watcher.exe")]  # loader + child
    assert [pid for pid, _ in watcher.installs_to_replace(OWN, running)] == [100, 101]


def test_the_same_exe_started_twice_is_not_replaced():
    """Our own onefile loader has our path too, so this also keeps it safe."""
    running = [(100, OWN), (101, OWN.lower()), (102, OWN.replace("\\", "/"))]
    assert watcher.installs_to_replace(OWN, running) == []


def test_nothing_running_means_nothing_to_replace():
    assert watcher.installs_to_replace(OWN, []) == []


@pytest.fixture
def lock_held_by(monkeypatch, tmp_path):
    """Fakes the Windows pieces: who is running, whether the lock frees once
    they are gone, and the kill itself."""
    monkeypatch.setattr(watcher, "LOG_PATH", str(tmp_path / "log.txt"))
    monkeypatch.setattr(watcher, "ERROR_LOG", str(tmp_path / "errors.txt"))
    monkeypatch.setattr(watcher.sys, "executable", OWN, raising=False)
    monkeypatch.setattr(watcher.time, "sleep", lambda _s: None)
    killed = []

    def arrange(running):
        alive = {pid for pid, _ in running}

        def kill(pid):
            killed.append(pid)
            alive.discard(pid)
            return True

        def lock_is_free():
            # The lock frees only once every other install is gone.
            return not any(os.path.normcase(p) != os.path.normcase(OWN) for pid, p in running if pid in alive)

        monkeypatch.setattr(watcher, "running_watchers", lambda: list(running))
        monkeypatch.setattr(watcher, "terminate_process", kill)
        monkeypatch.setattr(watcher, "release_single_instance", lambda: None)
        monkeypatch.setattr(watcher, "acquire_single_instance", lock_is_free)
        return killed
    return arrange


def test_the_old_install_is_ended_and_the_lock_taken(lock_held_by):
    old = r"C:\Users\me\Downloads\LanDashboardWatcher\savedvars_watcher.exe"
    killed = lock_held_by([(100, old), (101, old)])
    assert watcher.take_over_from_other_install() is True
    assert killed == [100, 101]


def test_a_double_click_of_the_same_exe_is_left_alone(lock_held_by):
    killed = lock_held_by([(100, OWN), (101, OWN)])
    assert watcher.take_over_from_other_install() is False
    assert killed == []


def test_an_install_that_will_not_die_does_not_hang_forever(lock_held_by, monkeypatch):
    old = r"C:\Users\me\Downloads\LanDashboardWatcher\savedvars_watcher.exe"
    lock_held_by([(100, old)])
    monkeypatch.setattr(watcher, "terminate_process", lambda _pid: False)
    clock = iter(range(0, 100))
    monkeypatch.setattr(watcher.time, "monotonic", lambda: next(clock))
    assert watcher.take_over_from_other_install() is False
