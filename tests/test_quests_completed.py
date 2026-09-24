"""The game's lifetime quest count, carried ahead of the profession list.

Two different numbers live side by side on purpose. "quests" is what this
dashboard has seen turned in -- it powers the XP breakdown, and it undercounts
anything that happened before the addon was installed or was lost in transit.
"quests_completed" is the game's own lifetime figure, read from statistic 98.

The awkward part is the wire format. The tail ends with a variable-length
profession list, so the new field cannot go on the end; it sits where
professions used to start. The two are told apart by shape -- a profession
always carries exactly two colons, a count carries none -- which is what lets
one server read both layouts during a rollout.
"""
import sqlite3

from conftest import accepted, leaderboard, rejected

STAMP = 1790000000
PROFS = "Herbalism:71:150,Tailoring:103:150"


def status(player="Ayla", tail=None):
    head = f"{player},STATUS,{STAMP},19,3445,19400,1234567,86400,3600"
    return f"{head},{tail}" if tail else head


def snapshot(module, player):
    connection = sqlite3.connect(module.DB_FILE)
    connection.row_factory = sqlite3.Row
    try:
        return dict(connection.execute(
            "SELECT * FROM player_snapshots WHERE player = ?", (player,)).fetchone())
    finally:
        connection.close()


# --- the new layout ------------------------------------------------------------

def test_the_lifetime_count_arrives_with_professions(build_server):
    _, client = build_server()
    accepted(client, status(tail=f"12.5,900,109,{PROFS}"))

    player = leaderboard(client)["Ayla"]
    assert player["quests_completed"] == 109
    assert [p["name"] for p in player["professions"]] == ["Herbalism", "Tailoring"]


def test_a_count_with_no_professions(build_server):
    _, client = build_server()
    accepted(client, status(tail="12.5,900,109,"))

    player = leaderboard(client)["Ayla"]
    assert player["quests_completed"] == 109
    assert player["professions"] == []


def test_zero_completed_is_a_real_answer(build_server):
    _, client = build_server()
    accepted(client, status(tail=f"12.5,900,0,{PROFS}"))
    assert leaderboard(client)["Ayla"]["quests_completed"] == 0


# --- the older layout, which must keep working during a rollout ----------------

def test_the_previous_layout_still_parses_as_professions(build_server):
    """v3.9.0 and earlier put professions straight at index 8. Reading the
    first one as a quest count would lose a profession and invent a number."""
    _, client = build_server()
    accepted(client, status(tail=f"12.5,900,{PROFS}"))

    player = leaderboard(client)["Ayla"]
    assert player["quests_completed"] is None
    assert [p["name"] for p in player["professions"]] == ["Herbalism", "Tailoring"]


def test_an_empty_slot_is_no_professions_not_a_withheld_count(build_server):
    """The older layout writes an empty field there to mean "this character
    has none". Treating it as a withheld quest count would invent a privacy
    choice nobody made."""
    _, client = build_server()
    accepted(client, status(tail="12.5,900,"))

    player = leaderboard(client)["Ayla"]
    assert player["professions"] == []
    assert player["quests_completed"] is None
    assert "quests_completed" not in player["private_fields"]


def test_a_six_field_status_still_works(build_server):
    _, client = build_server()
    accepted(client, status())
    assert leaderboard(client)["Ayla"]["quests_completed"] is None


def test_an_older_addon_does_not_wipe_a_known_count(build_server):
    """Absent is not zero. A character syncing from a machine with an older
    addon must not erase the figure already recorded."""
    _, client = build_server()
    accepted(client, status(tail=f"12.5,900,109,{PROFS}"))
    accepted(client, status(tail=f"12.5,900,{PROFS}"))

    assert leaderboard(client)["Ayla"]["quests_completed"] == 109


# --- both numbers coexist -------------------------------------------------------

def test_the_seen_count_and_the_lifetime_count_are_separate(build_server):
    """The dashboard's own tally drives the XP breakdown and cannot be
    replaced by the statistic, which carries no per-quest reward."""
    _, client = build_server()
    accepted(client, f"Ayla,QUEST,{STAMP},42,500")
    accepted(client, f"Ayla,QUEST,{STAMP},43,700")
    accepted(client, status(tail=f"12.5,900,109,{PROFS}"))

    row = next(p for p in client.get("/api/analytics", auth=("admin", "testpass")).json()["players"]
               if p["name"] == "Ayla")
    assert row["quests"] == 2, "what this dashboard has seen"
    assert row["quests_completed"] == 109, "what the game says the character has done"


def test_it_is_persisted_and_survives_a_restart(build_server):
    module, client = build_server()
    accepted(client, status(tail=f"12.5,900,109,{PROFS}"))
    assert snapshot(module, "Ayla")["quests_completed"] == 109

    _, restarted = build_server(DB_FILE=module.DB_FILE)
    assert leaderboard(restarted)["Ayla"]["quests_completed"] == 109


# --- refused --------------------------------------------------------------------

def test_a_nonsense_count_is_refused(build_server):
    _, client = build_server()
    rejected(client, status(tail=f"12.5,900,-5,{PROFS}"))
    rejected(client, status(tail=f"12.5,900,999999999,{PROFS}"))


def test_a_malformed_profession_is_still_refused_after_a_count(build_server):
    """The count must not turn the rest of the tail into a skip-anything zone."""
    _, client = build_server()
    rejected(client, status(tail="12.5,900,109,Herbalism:71"))
