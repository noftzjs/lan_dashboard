"""The watcher follows the file the player is actually using.

Discovery used to run once. A watcher that auto-started with Windows during
the beta kept watching _classic_beta_'s file after the game moved to
_forever_: green icon, nothing arriving. These cover the decision (which file
is live), the bookkeeping (positions kept per file, so a switch never
re-sends), and the version the addon leaves in the file.
"""
import json
import os
import time

import savedvars_watcher as watcher


def touch(path, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("LanDashboardDB = {}", encoding="utf-8")
    stamp = time.time() - age_seconds
    os.utime(path, (stamp, stamp))
    return str(path)


# --- which file is live ----------------------------------------------------------

def test_a_sibling_that_changed_more_recently_wins(tmp_path):
    beta = touch(tmp_path / "_classic_beta_" / "LanDashboard.lua", age_seconds=3600)
    forever = touch(tmp_path / "_forever_" / "LanDashboard.lua", age_seconds=10)
    assert watcher.newer_sibling(beta, [beta, forever]) == forever


def test_the_watched_file_stays_while_it_is_the_newest(tmp_path):
    beta = touch(tmp_path / "_classic_beta_" / "LanDashboard.lua", age_seconds=3600)
    forever = touch(tmp_path / "_forever_" / "LanDashboard.lua", age_seconds=10)
    assert watcher.newer_sibling(forever, [beta, forever]) is None


def test_the_same_file_spelled_differently_is_not_a_sibling(tmp_path):
    only = touch(tmp_path / "_forever_" / "LanDashboard.lua", age_seconds=10)
    assert watcher.newer_sibling(only, [only.lower(), only.replace("\\", "/")]) is None


def test_a_missing_watched_file_yields_to_any_real_one(tmp_path):
    """A pinned path that does not exist yet, or a file the game deleted."""
    gone = str(tmp_path / "_classic_beta_" / "LanDashboard.lua")
    forever = touch(tmp_path / "_forever_" / "LanDashboard.lua", age_seconds=10)
    assert watcher.newer_sibling(gone, [forever]) == forever


def test_nothing_else_found_means_no_switch(tmp_path):
    only = touch(tmp_path / "_forever_" / "LanDashboard.lua", age_seconds=10)
    assert watcher.newer_sibling(only, [only]) is None
    assert watcher.newer_sibling(only, []) is None


# --- positions kept per file --------------------------------------------------------

def test_switching_parks_the_old_position_and_restores_the_new_one():
    state = {"sent_count": 0, "last_sent": None, "path": None, "files": {}}
    assert watcher.switch_state_to(state, "A") is False      # first file: nothing to park
    state["sent_count"], state["last_sent"] = 40, "a40"

    assert watcher.switch_state_to(state, "B") is True
    assert (state["sent_count"], state["last_sent"]) == (0, None)
    state["sent_count"], state["last_sent"] = 7, "b7"

    assert watcher.switch_state_to(state, "A") is True
    assert (state["sent_count"], state["last_sent"]) == (40, "a40")
    assert state["files"]["B"] == {"sent_count": 7, "last_sent": "b7"}


def test_switching_to_the_same_file_changes_nothing():
    state = {"sent_count": 5, "last_sent": "x", "path": "A", "files": {}}
    assert watcher.switch_state_to(state, "A") is False
    assert (state["sent_count"], state["last_sent"]) == (5, "x")


def test_a_state_file_from_before_per_file_positions_keeps_its_count(tmp_path, monkeypatch):
    """Upgrading must not re-send a beta's worth of events."""
    legacy = tmp_path / ".savedvars_watcher_state.json"
    legacy.write_text(json.dumps({"sent_count": 812, "last_sent": "Cyk Null,XP,1,2,3"}), encoding="utf-8")
    monkeypatch.setattr(watcher, "STATE_PATH", str(legacy))

    state = watcher.load_state()
    assert watcher.switch_state_to(state, "C:/wow/_forever_/LanDashboard.lua") is False
    assert state["sent_count"] == 812
    assert state["path"] == "C:/wow/_forever_/LanDashboard.lua"


def test_the_state_round_trips_through_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "STATE_PATH", str(tmp_path / "state.json"))
    state = watcher.load_state()
    watcher.switch_state_to(state, "A")
    state["sent_count"] = 3
    watcher.switch_state_to(state, "B")
    watcher.save_state(state)

    again = watcher.load_state()
    assert again["path"] == "B"
    assert again["files"]["A"]["sent_count"] == 3


# --- the addon's version, read from the file ---------------------------------------

def test_the_addon_version_is_read_from_savedvariables():
    text = 'LanDashboardDB = {\n["version"] = "3.10.1",\n["events"] = {\n},\n}\n'
    assert watcher.parse_addon_version(text) == "3.10.1"


def test_an_older_addon_has_no_version():
    assert watcher.parse_addon_version('LanDashboardDB = {\n["events"] = {\n},\n}\n') is None


def test_a_version_that_is_not_a_version_is_ignored():
    text = 'LanDashboardDB = {\n["version"] = "3.10.1\\" .. evil",\n}\n'
    assert watcher.parse_addon_version(text) is None


# --- the addon's code file is not its save file ----------------------------------

def test_the_save_file_lives_in_savedvariables():
    assert watcher.looks_like_savedvars(r"C:\WoW\_forever_\WTF\Account\ACCT\SavedVariables\LanDashboard.lua")
    assert watcher.looks_like_savedvars("C:/WoW/_forever_/WTF/Account/ACCT/savedvariables/LanDashboard.lua")


def test_the_addon_code_file_is_rejected():
    """Same file name, wrong folder: the one a player picked in the tray dialog."""
    assert not watcher.looks_like_savedvars(r"C:\WoW\_forever_\Interface\AddOns\LanDashboard\LanDashboard.lua")
    assert not watcher.looks_like_savedvars("")
    assert not watcher.looks_like_savedvars(None)
