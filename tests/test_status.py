"""The STATUS snapshot: level, XP, gold and time played in one payload.

Gold and playtime have no change event worth listening to — gold moves on
every loot and vendor sale, and playtime only exists when the addon asks the
server for it — so they're sampled together at login and on level-up rather
than chased individually.
"""
import sqlite3

import pytest
from conftest import accepted, leaderboard, rejected

STAMP = 1790000000
GOLD = 1_234_567          # copper: 123g 45s 67c


def status(player="Ayla", level=18, current_xp=3445, max_xp=19400,
           gold=GOLD, played_total=86400, played_level=3600):
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


# --- the happy path ----------------------------------------------------------

def test_a_status_snapshot_sets_everything_at_once(build_server):
    module, client = build_server()
    accepted(client, status())

    player = leaderboard(client)["Ayla"]
    assert player["level"] == 18
    assert player["pct"] == pytest.approx(17.76, abs=0.01)
    assert player["gold"] == GOLD
    assert player["played_total"] == 86400
    assert player["played_level"] == 3600
    assert snapshot(module, "Ayla")["gold"] == GOLD


def test_status_carries_level_for_a_capped_character(server):
    """The reason STATUS exists at all: a character that cannot gain XP never
    fires an XP event, so this is the only thing that reports its level."""
    accepted(server, status(player="Capped", level=20, current_xp=0, max_xp=0))
    player = leaderboard(server)["Capped"]
    assert player["level"] == 20
    assert player["max_xp"] == 0
    assert player["pct"] == 100.0


def test_gold_is_stored_in_copper_without_rounding(server):
    """Converting to gold is the dashboard's job; the server must not lose
    the silver and copper on the way in."""
    accepted(server, status(gold=1))
    assert leaderboard(server)["Ayla"]["gold"] == 1


def test_a_later_snapshot_replaces_the_earlier_one(server):
    accepted(server, status(gold=100, played_total=3600))
    accepted(server, status(gold=250, played_total=7200))
    player = leaderboard(server)["Ayla"]
    assert (player["gold"], player["played_total"]) == (250, 7200)


def test_playtime_of_zero_is_recorded_as_unknown(server):
    """The addon sends 0 when the server hasn't answered its playtime request
    yet. That is "no answer", not "zero seconds played", and charting it as
    zero would drag any average down."""
    accepted(server, status(played_total=0, played_level=0))
    player = leaderboard(server)["Ayla"]
    assert player["played_total"] is None
    assert player["played_level"] is None
    assert player["gold"] == GOLD          # the rest of the snapshot still lands


# --- validation --------------------------------------------------------------

@pytest.mark.parametrize("payload, expected", [
    (status(level=99), "Level"),
    (status(gold=-1), "gold"),
    (status(gold=99_999_999_999), "gold"),
    (status(current_xp=999, max_xp=100), "exceeds"),
    (status(played_total=-5), "played_total"),
    (status(played_total=999_999_999_999), "played_total"),
    # a per-level timer cannot exceed the lifetime one
    (status(played_total=100, played_level=500), "played_level"),
    (f"Short,STATUS,{STAMP},18,100,200", "6 fields"),
])
def test_bad_snapshots_are_rejected(server, payload, expected):
    assert expected.lower() in rejected(server, payload).lower()


def test_a_rejected_snapshot_leaves_the_previous_one_intact(server):
    accepted(server, status(gold=500))
    rejected(server, status(gold=-1))
    assert leaderboard(server)["Ayla"]["gold"] == 500


# --- history -----------------------------------------------------------------

def test_each_snapshot_is_kept_in_history_for_charting(build_server):
    module, client = build_server()
    accepted(client, status(gold=100, played_total=3600))
    accepted(client, status(gold=900, played_total=7200))

    connection = sqlite3.connect(module.DB_FILE)
    connection.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in connection.execute(
            "SELECT gold, played_total, event_time FROM xp_history"
            " WHERE log_type = 'STATUS' ORDER BY id")]
    finally:
        connection.close()

    assert [r["gold"] for r in rows] == [100, 900]
    assert [r["played_total"] for r in rows] == [3600, 7200]
    assert all(r["event_time"] for r in rows)


def test_status_survives_a_restart(build_server):
    import asyncio
    module, client = build_server()
    accepted(client, status(gold=4242, played_total=12345))

    module.player_states.clear()
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        module.reload_states_from_db())

    restored = module.player_states["Ayla"]
    assert restored["gold"] == 4242
    assert restored["played_total"] == 12345


def test_analytics_reports_gold_and_playtime(server):
    """The roster table on /analytics reads these straight off the payload,
    so they have to be in it -- they were briefly missing and the column
    rendered an em dash for every character."""
    from conftest import ROSTER_AUTH
    accepted(server, status(gold=987654, played_total=144000, played_level=7200))
    data = server.get("/api/analytics", auth=ROSTER_AUTH).json()
    player = next(p for p in data["players"] if p["name"] == "Ayla")
    assert player["gold"] == 987654
    assert player["played_total"] == 144000
    assert player["played_level"] == 7200


def test_analytics_reports_absent_gold_as_none(server):
    accepted(server, f"NoStatus,XP,{STAMP},10,100,1000")
    from conftest import ROSTER_AUTH
    data = server.get("/api/analytics", auth=ROSTER_AUTH).json()
    player = next(p for p in data["players"] if p["name"] == "NoStatus")
    assert player["gold"] is None
    assert player["played_total"] is None


def test_status_counts_as_activity(server):
    """A capped character with nothing else to report still has to look alive
    rather than drifting into the idle filter."""
    accepted(server, status())
    assert leaderboard(server)["Ayla"]["idle_seconds"] == pytest.approx(0, abs=5)


# --- compatibility -----------------------------------------------------------

def test_a_pre_stamp_status_still_parses(server):
    """An addon sending STATUS without the in-game timestamp."""
    accepted(server, f"Old,STATUS,18,3445,19400,{GOLD},86400,3600")
    assert leaderboard(server)["Old"]["gold"] == GOLD


def test_plain_xp_events_still_work_alongside_status(server):
    """STATUS doesn't replace the XP event that fires while levelling."""
    accepted(server, status(level=10, current_xp=100, max_xp=7600, gold=50))
    accepted(server, f"Ayla,XP,{STAMP + 60},10,900,7600")
    player = leaderboard(server)["Ayla"]
    assert player["current_xp"] == 900
    assert player["gold"] == 50          # not clobbered by the XP event
