"""The /api/log-update parser: the one place untrusted text from the game
becomes stored state, and the thing most likely to break quietly."""
import pytest
from conftest import accepted, leaderboard, rejected

STAMP = 1790000000          # 2026-09-21T14:13:20Z, inside the sane range


# --- the four original payload shapes ----------------------------------------

def test_profile_zone_xp_and_quest_round_trip(server):
    accepted(server, f"Ayla,PROFILE,{STAMP},Alliance,PRIEST,Lan Gang")
    accepted(server, f"Ayla,ZONE,{STAMP},Elwynn Forest")
    accepted(server, f"Ayla,XP,{STAMP},12,5000,10100")
    accepted(server, f"Ayla,QUEST,{STAMP},8342,675")

    player = leaderboard(server)["Ayla"]
    assert player["faction"] == "Alliance"
    assert player["class"] == "PRIEST"
    assert player["guild"] == "Lan Gang"
    assert player["current_zone"] == "Elwynn Forest"
    assert (player["level"], player["current_xp"], player["max_xp"]) == (12, 5000, 10100)
    assert player["pct"] == pytest.approx(49.5, abs=0.01)


def test_a_player_first_seen_through_a_quest_still_appears(server):
    """QUEST writes to the history table, not the snapshot — it still has to
    create the character, or a quest-first sync would vanish."""
    accepted(server, f"QuestFirst,QUEST,{STAMP},101,250")
    assert "QuestFirst" in leaderboard(server)


def test_zone_and_profile_arriving_before_any_xp(server):
    """Real login order. These used to be dropped for an unknown player."""
    accepted(server, f"Fresh,ZONE,{STAMP},Durotar")
    accepted(server, f"Fresh,PROFILE,{STAMP},Horde,SHAMAN,")
    player = leaderboard(server)["Fresh"]
    assert player["current_zone"] == "Durotar"
    assert player["level"] == 1          # the default, no XP event yet


# --- free-text fields that may contain commas --------------------------------

def test_guild_name_containing_commas_survives(server):
    accepted(server, f"Comma,PROFILE,{STAMP},Alliance,MAGE,Knights, Inc")
    assert leaderboard(server)["Comma"]["guild"] == "Knights, Inc"


def test_zone_name_containing_commas_survives(server):
    accepted(server, f"Comma,ZONE,{STAMP},Somewhere, Odd")
    assert leaderboard(server)["Comma"]["current_zone"] == "Somewhere, Odd"


def test_empty_guild_is_stored_as_absent_not_as_a_name(server):
    """A guildless character must be distinguishable from one whose guild is
    literally called "No Guild" — that ambiguity was a real bug."""
    accepted(server, f"Solo,PROFILE,{STAMP},Horde,ROGUE,")
    assert leaderboard(server)["Solo"]["guild"] is None


def test_a_guild_really_named_no_guild_is_preserved(server):
    accepted(server, f"Literal,PROFILE,{STAMP},Horde,ROGUE,No Guild")
    assert leaderboard(server)["Literal"]["guild"] == "No Guild"


# --- validation --------------------------------------------------------------

@pytest.mark.parametrize("payload, expected", [
    (f"Bad,PROFILE,{STAMP},Scourge,PRIEST,", "faction"),
    (f"Bad,PROFILE,{STAMP},Alliance,NECROMANCER,", "class"),
    (f"Bad,XP,{STAMP},99,10,100", "Level"),
    (f"Bad,XP,{STAMP},0,10,100", "Level"),
    (f"Bad,XP,{STAMP},10,-5,100", "current_xp"),
    (f"Bad,XP,{STAMP},10,999,100", "exceeds"),
    (f"Bad,XP,{STAMP},10,10,-1", "max_xp"),
    (f"Bad,BOGUS,{STAMP},1,2", "Unknown log_type"),
    ("", "missing name/type"),
])
def test_malformed_payloads_are_rejected(server, payload, expected):
    assert expected.lower() in rejected(server, payload).lower()


def test_over_long_fields_are_rejected(server):
    assert "too long" in rejected(server, f"{'N' * 25},ZONE,{STAMP},Somewhere").lower()
    assert "too long" in rejected(server, f"Ok,ZONE,{STAMP},{'Z' * 101}").lower()
    assert "too long" in rejected(server, f"Ok,PROFILE,{STAMP},Horde,MAGE,{'G' * 101}").lower()


def test_a_rejected_payload_does_not_count_as_activity(build_server):
    """Staleness keys off last_activity; a refused payload must not refresh
    it, or a client sending junk could keep a dead character looking alive.

    Compares the stored stamp rather than the derived idle_seconds, which
    differ by microseconds here and would make this pass by luck.
    """
    module, client = build_server()
    accepted(client, f"Quiet,XP,{STAMP},10,100,1000")
    before = module.player_states["Quiet"]["last_activity"]

    rejected(client, f"Quiet,PROFILE,{STAMP},Scourge,PRIEST,")
    rejected(client, f"Quiet,XP,{STAMP},99,100,1000")

    assert module.player_states["Quiet"]["last_activity"] == before


# --- the level cap (a character that cannot gain XP) -------------------------

def test_max_level_character_is_accepted_and_reads_as_complete(server):
    """At the cap the game reports 0 XP required. That used to be rejected,
    which left a capped character stuck on the default level 1."""
    accepted(server, f"Capped,XP,{STAMP},20,0,0")
    player = leaderboard(server)["Capped"]
    assert player["level"] == 20
    assert player["max_xp"] == 0
    assert player["pct"] == 100.0


def test_negative_max_xp_is_still_rejected(server):
    """Accepting 0 must not have opened the door to nonsense."""
    assert "max_xp" in rejected(server, f"Capped,XP,{STAMP},20,0,-1")


# --- in-game event timestamps (addon v2.11.0+) -------------------------------

def test_event_time_is_recorded_separately_from_delivery_time(build_server):
    module, client = build_server()
    accepted(client, f"Timed,XP,{STAMP},12,100,1000", timestamp="2026-09-23T12:00:00")
    row = _history(module, "Timed")[0]
    assert row["event_time"].startswith("2026-09-21T14:13:20")
    assert row["timestamp"] == "2026-09-23T12:00:00"
    assert row["event_time"] != row["timestamp"]


def test_a_pre_v2_11_addon_still_works_without_a_stamp(build_server):
    module, client = build_server()
    accepted(client, "Legacy,XP,12,100,1000")
    accepted(client, "Legacy,ZONE,Ironforge")
    assert _history(module, "Legacy")[0]["event_time"] is None
    assert leaderboard(client)["Legacy"]["current_zone"] == "Ironforge"


@pytest.mark.parametrize("leading, expected_zone", [
    ("123456789", "123456789,Ironforge"),      # 9 digits: a quest id, not a stamp
    ("1234567890", "1234567890,Ironforge"),    # 10 digits but year 2009 — out of range
])
def test_numbers_that_are_not_timestamps_are_left_alone(server, leading, expected_zone):
    accepted(server, f"Edge,ZONE,{leading},Ironforge")
    assert leaderboard(server)["Edge"]["current_zone"] == expected_zone


def test_one_delivery_batch_keeps_distinct_event_times(build_server):
    """The whole point of the stamp: a sync flushes many events at once, and
    they must not collapse onto the single instant they were delivered."""
    module, client = build_server()
    for index in range(4):
        accepted(client, f"Batch,XP,{STAMP + index * 1800},12,{100 + index},1000",
                 timestamp="2026-09-23T12:00:00")
    rows = _history(module, "Batch")
    assert len({r["timestamp"] for r in rows}) == 1
    assert len({r["event_time"] for r in rows}) == 4


# --- deaths ------------------------------------------------------------------

def test_deaths_are_counted_per_character(build_server):
    module, client = build_server()
    accepted(client, f"Dier,DEATH,{STAMP},19,Deadmines")
    accepted(client, f"Dier,DEATH,{STAMP + 60},19,Duskwood")
    rows = _history(module, "Dier", log_type="DEATH")
    assert len(rows) == 2
    assert rows[0]["level"] == 19


def test_a_death_validates_its_level_like_everything_else(server):
    assert "Level" in rejected(server, f"Dier,DEATH,{STAMP},99,Deadmines")


def test_death_accepts_the_pre_stamp_shape(server):
    accepted(server, "OldDier,DEATH,19,Deadmines")


def _history(module, player, log_type="XP"):
    """Rows straight from the history table — the endpoint doesn't expose it."""
    import sqlite3
    connection = sqlite3.connect(module.DB_FILE)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute(
            "SELECT * FROM xp_history WHERE player = ? AND log_type = ? ORDER BY id",
            (player, log_type))]
    finally:
        connection.close()
