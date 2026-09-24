"""The STATUS tail: item level, AFK seconds and professions (addon v3.3.0+).

Appended after played_level rather than sent as a new log type, which is what
makes it safe to deploy in any order. An older watcher forwards the line
regardless (it tests len(parts) >= 8, and extra fields still pass) and an
older server slices parts[:6] and ignores the tail. The mirror of that
obligation is tested here: this server must keep accepting a six-field STATUS
from a tester who has not updated their addon.

The API was settled by probing the real client (see README, 2026-09-24):
GetProfessions is present and returns name/rank/maxRank, and profession names
can contain spaces ("First Aid") but never commas or colons -- which is what
makes name:rank:maxRank unambiguous inside a comma-separated payload.
"""
import json
import sqlite3

from conftest import accepted, leaderboard, rejected

STAMP = 1790000000
PROFS = "Herbalism:71:150,Tailoring:103:150,First Aid:1:75"


def status(player="Ayla", level=18, current_xp=3445, max_xp=19400, gold=1234567,
           played_total=86400, played_level=3600, tail="12.5,900," + PROFS):
    head = (f"{player},STATUS,{STAMP},{level},{current_xp},{max_xp},"
            f"{gold},{played_total},{played_level}")
    return f"{head},{tail}" if tail else head


def snapshot(module, player):
    connection = sqlite3.connect(module.DB_FILE)
    connection.row_factory = sqlite3.Row
    try:
        return dict(connection.execute(
            "SELECT * FROM player_snapshots WHERE player = ?", (player,)).fetchone())
    finally:
        connection.close()


# --- the tail arrives ---------------------------------------------------------

def test_the_tail_carries_item_level_afk_and_professions(build_server):
    _, client = build_server()
    accepted(client, status())

    player = leaderboard(client)["Ayla"]
    assert player["item_level"] == 12.5
    assert player["afk_total"] == 900
    assert player["professions"] == [
        {"name": "Herbalism", "rank": 71, "max_rank": 150},
        {"name": "Tailoring", "rank": 103, "max_rank": 150},
        {"name": "First Aid", "rank": 1, "max_rank": 75},
    ]


def test_a_profession_name_may_contain_spaces(build_server):
    """"First Aid" is why the separator is a colon and not a space."""
    _, client = build_server()
    accepted(client, status(tail="12.5,0,First Aid:42:75"))

    assert leaderboard(client)["Ayla"]["professions"] == [
        {"name": "First Aid", "rank": 42, "max_rank": 75}]


def test_a_character_with_no_professions_is_fine(build_server):
    _, client = build_server()
    accepted(client, status(tail="12.5,0,"))

    player = leaderboard(client)["Ayla"]
    assert player["professions"] == []
    assert player["item_level"] == 12.5


def test_item_level_keeps_its_fraction(build_server):
    """It is a float on this client -- 12.5 at level 18 -- so rounding it to an
    int would quietly lose half a point of gear."""
    _, client = build_server()
    accepted(client, status(tail="23.75,0,"))
    assert leaderboard(client)["Ayla"]["item_level"] == 23.75


# --- older addons -------------------------------------------------------------

def test_a_six_field_status_from_an_older_addon_is_still_accepted(build_server):
    """Testers do not all update at once; this is the direction that must not
    break."""
    _, client = build_server()
    accepted(client, status(tail=None))

    player = leaderboard(client)["Ayla"]
    assert player["level"] == 18
    assert player["gold"] == 1234567
    assert player["item_level"] is None
    assert player["afk_total"] is None
    assert player["professions"] == []


def test_an_older_addon_does_not_wipe_professions_already_known(build_server):
    """Absent is not the same as empty. A downgrade, or one character syncing
    from a machine with an older addon, must not erase what is known."""
    _, client = build_server()
    accepted(client, status())
    accepted(client, status(tail=None))

    assert len(leaderboard(client)["Ayla"]["professions"]) == 3


def test_a_partial_tail_is_read_as_far_as_it_goes(build_server):
    _, client = build_server()
    accepted(client, status(tail="15.0"))

    player = leaderboard(client)["Ayla"]
    assert player["item_level"] == 15.0
    assert player["afk_total"] is None


# --- withholding generalises --------------------------------------------------

def test_item_level_can_be_withheld_like_gold(build_server):
    _, client = build_server()
    accepted(client, status(tail=",900," + PROFS))

    player = leaderboard(client)["Ayla"]
    assert player["item_level"] is None
    assert player["private_fields"] == ["item_level"]


def test_several_fields_can_be_withheld_at_once(build_server):
    _, client = build_server()
    accepted(client, status(gold="", tail=",,"))

    player = leaderboard(client)["Ayla"]
    assert sorted(player["private_fields"]) == ["afk_total", "gold", "item_level"]


# --- persistence --------------------------------------------------------------

def test_the_tail_is_persisted_and_survives_a_restart(build_server):
    module, client = build_server()
    accepted(client, status())

    row = snapshot(module, "Ayla")
    assert row["item_level"] == 12.5
    assert row["afk_total"] == 900
    assert len(json.loads(row["professions"])) == 3

    _, restarted = build_server(DB_FILE=module.DB_FILE)
    player = leaderboard(restarted)["Ayla"]
    assert player["item_level"] == 12.5
    assert player["professions"][0]["name"] == "Herbalism"


# --- what is refused ----------------------------------------------------------

def test_a_malformed_profession_is_refused_not_skipped(build_server):
    """Dropping it silently would hide an addon bug behind a dashboard that
    merely looks a little empty."""
    _, client = build_server()
    rejected(client, status(tail="12.5,0,Herbalism"))
    rejected(client, status(tail="12.5,0,Herbalism:71"))
    rejected(client, status(tail="12.5,0,Herbalism:71:150:extra"))
    rejected(client, status(tail="12.5,0,:71:150"))


def test_impossible_profession_ranks_are_refused(build_server):
    _, client = build_server()
    rejected(client, status(tail="12.5,0,Herbalism:200:150"))
    rejected(client, status(tail="12.5,0,Herbalism:-5:150"))
    rejected(client, status(tail="12.5,0,Herbalism:71:99999"))


def test_afk_above_total_playtime_is_refused(build_server):
    """The addon counts AFK itself, so there is no authority behind it -- more
    AFK than playtime means the counter is broken, not a very idle player."""
    _, client = build_server()
    rejected(client, status(played_total=3600, played_level=60, tail="12.5,7200,"))


def test_nonsense_item_level_is_refused(build_server):
    _, client = build_server()
    rejected(client, status(tail="-1,0,"))
    rejected(client, status(tail="99999,0,"))
    rejected(client, status(tail="notanumber,0,"))


def test_an_absurd_number_of_professions_is_refused(build_server):
    _, client = build_server()
    many = ",".join(f"Prof{i}:1:75" for i in range(11))
    rejected(client, status(tail=f"12.5,0,{many}"))


# --- the analytics page reads these too ---------------------------------------

def test_the_analytics_payload_carries_the_tail_and_privacy(build_server):
    """The analytics endpoint builds its rows field by field rather than
    spreading state, so a new field is invisible there until it is added by
    hand -- which has already caused one round of em dashes on that page, and
    would silently turn a withheld gold value back into "no data yet"."""
    _, client = build_server()
    accepted(client, status(gold="", tail="12.5,900," + PROFS))

    row = next(p for p in client.get(
        "/api/analytics", auth=("admin", "testpass")).json()["players"]
        if p["name"] == "Ayla")
    assert row["item_level"] == 12.5
    assert row["afk_total"] == 900
    assert [p["name"] for p in row["professions"]] == ["Herbalism", "Tailoring", "First Aid"]
    assert row["private_fields"] == ["gold"]
