"""Deaths, split by level and by the zone they happened in.

The zone rode along in the DEATH payload from the start but was validated and
then discarded -- there was no column for it. Deaths recorded before that
column existed have no zone, so the two views cover different amounts of
history and the page has to be able to say so.
"""
import sqlite3

from conftest import accepted, rejected

STAMP = 1790000000


def death(player, level, zone, at=None):
    return f"{player},DEATH,{at or STAMP},{level},{zone}"


def analytics(client):
    return client.get("/api/analytics", auth=("admin", "testpass")).json()


def test_a_death_records_where_it_happened(build_server):
    module, client = build_server()
    accepted(client, death("Ayla", 12, "Duskwood"))

    connection = sqlite3.connect(module.DB_FILE)
    try:
        stored = connection.execute(
            "SELECT zone FROM xp_history WHERE log_type='DEATH'").fetchone()[0]
    finally:
        connection.close()
    assert stored == "Duskwood"


def test_deaths_group_by_zone_most_lethal_first(build_server):
    """Zone names chosen so alphabetical order and death order disagree: the
    deadliest zone sorts last by name. An earlier version of this test used
    names where the two happened to coincide, so it passed even with the
    ordering removed entirely."""
    _, client = build_server()
    for _ in range(4):
        accepted(client, death("Ayla", 12, "Westfall"))
    accepted(client, death("Bo", 9, "Ashenvale"))
    accepted(client, death("Cy", 14, "Duskwood"))
    accepted(client, death("Cy", 15, "Duskwood"))

    assert analytics(client)["deaths_by_zone"] == [
        {"zone": "Westfall", "deaths": 4},
        {"zone": "Duskwood", "deaths": 2},
        {"zone": "Ashenvale", "deaths": 1},
    ]


def test_zones_tied_on_deaths_are_ordered_by_name(build_server):
    """So the list is stable between refreshes rather than shuffling."""
    _, client = build_server()
    accepted(client, death("Ayla", 12, "Westfall"))
    accepted(client, death("Bo", 9, "Ashenvale"))

    zones = [z["zone"] for z in analytics(client)["deaths_by_zone"]]
    assert zones == ["Ashenvale", "Westfall"]


def test_deaths_group_by_level(build_server):
    _, client = build_server()
    accepted(client, death("Ayla", 12, "Duskwood"))
    accepted(client, death("Bo", 12, "Westfall"))
    accepted(client, death("Cy", 20, "Stranglethorn Vale"))

    assert analytics(client)["deaths_by_level"] == [
        {"level": 12, "deaths": 2},
        {"level": 20, "deaths": 1},
    ]


def test_a_zone_name_with_a_comma_survives(build_server):
    """Zone is the remainder of the payload, not a fixed field, so a comma in
    the name stays part of it rather than truncating it."""
    _, client = build_server()
    accepted(client, death("Ayla", 12, "Somewhere, Somewhere Else"))

    assert analytics(client)["deaths_by_zone"][0]["zone"] == "Somewhere, Somewhere Else"


def test_older_deaths_without_a_zone_are_counted_separately(build_server):
    """They are still deaths and still belong in the by-level view; they just
    cannot be placed. Folding them into a zone bucket would invent a fact."""
    module, client = build_server()
    accepted(client, death("Ayla", 12, "Duskwood"))

    # A death as it was stored before the column existed.
    connection = sqlite3.connect(module.DB_FILE)
    try:
        connection.execute(
            "INSERT INTO xp_history (timestamp, player, log_type, level, zone)"
            " VALUES ('old', 'Ayla', 'DEATH', 7, NULL)")
        connection.commit()
    finally:
        connection.close()

    data = analytics(client)
    assert data["deaths_without_zone"] == 1
    assert data["deaths_by_zone"] == [{"zone": "Duskwood", "deaths": 1}]
    # The zoneless death still counts at its level.
    assert {"level": 7, "deaths": 1} in data["deaths_by_level"]


def test_a_death_with_no_zone_in_the_payload_is_not_a_blank_zone(build_server):
    """An empty remainder means "not reported", which must not become a zone
    whose name is the empty string."""
    _, client = build_server()
    accepted(client, f"Ayla,DEATH,{STAMP},12,")

    data = analytics(client)
    assert data["deaths_by_zone"] == []
    assert data["deaths_without_zone"] == 1


def test_an_impossible_death_level_is_still_refused(build_server):
    _, client = build_server()
    rejected(client, death("Ayla", 99, "Duskwood"))
    rejected(client, death("Ayla", 0, "Duskwood"))


def test_no_deaths_reports_empty_rather_than_failing(build_server):
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")

    data = analytics(client)
    assert data["deaths_by_zone"] == []
    assert data["deaths_by_level"] == []
    assert data["deaths_without_zone"] == 0
