"""Zone history, and who was playing when.

Two things that were previously unanswerable and are now recorded:

Zone changes used to overwrite a single "where is this character now" value
and were never kept, so there was no way to ask where a weekend was spent --
and once the LAN is over, that data does not come back.

Activity is bucketed by the hour an event happened in, across every stored
event type. It measures "was this person playing", not "was this person
levelling", which is why it counts quests, deaths and status snapshots too.
"""
import sqlite3

from conftest import accepted

STAMP = 1790000000       # 2026-09-22T02:13:20Z-ish; exact hour asserted below


def analytics(client):
    return client.get("/api/analytics", auth=("admin", "testpass")).json()


def rows(module, log_type):
    connection = sqlite3.connect(module.DB_FILE)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute(
            "SELECT * FROM xp_history WHERE log_type = ? ORDER BY id", (log_type,))]
    finally:
        connection.close()


# --- zone history -------------------------------------------------------------

def test_a_zone_change_is_kept_as_history(build_server):
    module, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Elwynn Forest")

    stored = rows(module, "ZONE")
    assert len(stored) == 1
    assert stored[0]["zone"] == "Elwynn Forest"
    assert stored[0]["event_time"] is not None


def test_moving_between_zones_records_each_one(build_server):
    """The sequence is the point -- one row per zone, in order, is what makes
    time-per-zone derivable later."""
    module, client = build_server()
    for i, zone in enumerate(["Elwynn Forest", "Westfall", "Duskwood"]):
        accepted(client, f"Ayla,ZONE,{STAMP + i * 600},{zone}")

    assert [r["zone"] for r in rows(module, "ZONE")] == [
        "Elwynn Forest", "Westfall", "Duskwood"]


def test_the_current_zone_still_tracks_the_latest(build_server):
    """History is additional to the existing behaviour, not a replacement."""
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Elwynn Forest")
    accepted(client, f"Ayla,ZONE,{STAMP + 600},Westfall")

    board = {p["name"]: p for p in client.get("/api/leaderboard").json()}
    assert board["Ayla"]["current_zone"] == "Westfall"


def test_returning_to_a_zone_is_a_separate_visit(build_server):
    """Not deduplicated: going back to Ironforge twice is two visits, and
    collapsing them would understate time spent there."""
    module, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
    accepted(client, f"Ayla,ZONE,{STAMP + 600},Loch Modan")
    accepted(client, f"Ayla,ZONE,{STAMP + 1200},Ironforge")

    assert [r["zone"] for r in rows(module, "ZONE")] == ["Ironforge", "Loch Modan", "Ironforge"]


# --- activity ------------------------------------------------------------------

def test_activity_counts_events_in_the_hour_they_happened(build_server):
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
    accepted(client, f"Ayla,ZONE,{STAMP + 60},Loch Modan")

    activity = analytics(client)["activity"]["Ayla"]
    assert sum(activity.values()) == 2
    assert len(activity) == 1, "both events fall in the same hour"


def test_activity_separates_different_hours(build_server):
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
    accepted(client, f"Ayla,ZONE,{STAMP + 7200},Loch Modan")

    activity = analytics(client)["activity"]["Ayla"]
    assert len(activity) == 2
    assert sorted(activity.values()) == [1, 1]


def test_activity_counts_every_kind_of_event_not_just_levels(build_server):
    """It answers "was this person playing", so a quiet hour of questing
    counts as much as a level-up."""
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
    accepted(client, f"Ayla,QUEST,{STAMP},42,500")
    accepted(client, f"Ayla,DEATH,{STAMP},9,Ironforge")

    assert sum(analytics(client)["activity"]["Ayla"].values()) == 3


def test_activity_keeps_characters_apart(build_server):
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
    accepted(client, f"Bo,ZONE,{STAMP},Orgrimmar")
    accepted(client, f"Bo,ZONE,{STAMP + 60},Durotar")

    activity = analytics(client)["activity"]
    assert sum(activity["Ayla"].values()) == 1
    assert sum(activity["Bo"].values()) == 2


def test_the_hour_list_spans_every_hour_seen(build_server):
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
    accepted(client, f"Bo,ZONE,{STAMP + 7200},Orgrimmar")

    hours = analytics(client)["activity_hours"]
    assert len(hours) == 2
    assert hours == sorted(hours), "sorted so the page can use them as an axis"


def test_untimed_events_are_left_out_of_activity(build_server):
    """An event with no in-game clock cannot be placed on a time axis, and
    guessing would put it in the wrong hour."""
    _, client = build_server()
    accepted(client, "Ayla,ZONE,Ironforge")        # no timestamp field

    assert analytics(client)["activity"] == {}
