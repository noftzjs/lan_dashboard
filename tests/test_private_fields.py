"""Opting out of sharing a STATUS field, starting with gold.

The dashboard is public by design, and a tester asked not to have their gold
on it. STATUS is positional, so a declined field can't simply be dropped
without shifting everything after it -- an empty slot is sent instead.

This is the *server* half, and it ships alone on purpose. Nothing sends an
empty field yet. A rejected event advances the watcher's sent_count and is
destroyed permanently, so if the addon shipped first, every withheld status
between the two deploys would be lost. Teaching the server to accept it first
makes the addon half safe to deploy in either order.
"""
import json
import sqlite3

from conftest import accepted, leaderboard, rejected

STAMP = 1790000000


def status(player="Ayla", level=18, current_xp=3445, max_xp=19400,
           gold="1234567", played_total=86400, played_level=3600):
    """gold is a string so a test can pass "" -- the withheld marker."""
    return (f"{player},STATUS,{STAMP},{level},{current_xp},{max_xp},"
            f"{gold},{played_total},{played_level}")


def snapshot(module, player):
    connection = sqlite3.connect(module.DB_FILE)
    connection.row_factory = sqlite3.Row
    try:
        return dict(connection.execute(
            "SELECT * FROM player_snapshots WHERE player = ?", (player,)).fetchone())
    finally:
        connection.close()


# --- withholding a field ------------------------------------------------------

def test_an_empty_gold_field_is_accepted_not_rejected(build_server):
    """The whole point: this used to raise on int("") and destroy the event."""
    _, client = build_server()
    accepted(client, status(gold=""))

    player = leaderboard(client)["Ayla"]
    assert player["gold"] is None
    assert player["private_fields"] == ["gold"]


def test_withholding_gold_does_not_disturb_the_fields_after_it(build_server):
    """Positional payload: the empty slot must not shift played time."""
    _, client = build_server()
    accepted(client, status(gold="", played_total=86400, played_level=3600))

    player = leaderboard(client)["Ayla"]
    assert player["level"] == 18
    assert player["played_total"] == 86400
    assert player["played_level"] == 3600


def test_sharing_gold_reports_nothing_as_private(build_server):
    _, client = build_server()
    accepted(client, status(gold="500"))

    player = leaderboard(client)["Ayla"]
    assert player["gold"] == 500
    assert player["private_fields"] == []


def test_zero_gold_is_a_real_value_not_a_withheld_one(build_server):
    """A character genuinely can be broke, which is why 0 can't be the marker
    the way it is for played_total."""
    _, client = build_server()
    accepted(client, status(gold="0"))

    player = leaderboard(client)["Ayla"]
    assert player["gold"] == 0
    assert player["private_fields"] == []


def test_withheld_is_distinguishable_from_never_reported(build_server):
    """Both store NULL gold, so without private_fields the page could not tell
    "chose not to share" from "no status yet" -- and they should not read the
    same."""
    _, client = build_server()
    accepted(client, f"Never,ZONE,{STAMP},Ironforge")
    accepted(client, status(player="Withheld", gold=""))

    board = leaderboard(client)
    assert board["Never"]["gold"] is None
    assert board["Never"]["private_fields"] == []
    assert board["Withheld"]["gold"] is None
    assert board["Withheld"]["private_fields"] == ["gold"]


# --- persistence --------------------------------------------------------------

def test_the_choice_is_persisted_and_survives_a_restart(build_server):
    module, client = build_server()
    accepted(client, status(gold=""))
    assert json.loads(snapshot(module, "Ayla")["private_fields"]) == ["gold"]

    _, restarted = build_server(DB_FILE=module.DB_FILE)
    assert leaderboard(restarted)["Ayla"]["private_fields"] == ["gold"]


def test_sharing_again_clears_the_private_flag(build_server):
    """Turning the setting back off must not leave the field marked private."""
    _, client = build_server()
    accepted(client, status(gold=""))
    accepted(client, status(gold="900"))

    player = leaderboard(client)["Ayla"]
    assert player["gold"] == 900
    assert player["private_fields"] == []


# --- what is still refused ----------------------------------------------------

def test_only_gold_may_be_withheld_so_far(build_server):
    """Emptying any other field is still malformed, not a privacy choice --
    guarding against a bug that silently swallows a broken payload."""
    _, client = build_server()
    rejected(client, status(level=""))
    rejected(client, status(current_xp=""))
    rejected(client, status(played_total=""))
    rejected(client, status(played_level=""))


def test_garbage_gold_is_still_refused(build_server):
    _, client = build_server()
    rejected(client, status(gold="notanumber"))
    rejected(client, status(gold="-5"))
    rejected(client, status(gold="99999999999"))
