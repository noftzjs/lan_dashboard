"""How long each level took, measured in played time rather than wall clock.

Wall-clock between level-ups would count sleep: on a LAN weekend that turns an
ordinary level into a twelve-hour one purely because the player went to bed
during it. played_total is the game's own count of time actually logged in, so
the difference between two level-ups is the time the level really cost.

It rides on STATUS rows, and the addon requests a STATUS on every
PLAYER_LEVEL_UP, so there is a sample at each transition.
"""
from conftest import accepted

STAMP = 1790000000


def status(player, level, played, at=None, gold=500):
    """A STATUS as the addon sends one at a level-up."""
    when = STAMP if at is None else at
    return f"{player},STATUS,{when},{level},0,19400,{gold},{played},60"


def level_times(client):
    return client.get("/api/analytics", auth=("admin", "testpass")).json()["level_times"]


def test_a_level_costs_the_played_time_between_its_ends(build_server):
    _, client = build_server()
    accepted(client, status("Ayla", 5, 3600, at=STAMP))
    accepted(client, status("Ayla", 6, 7200, at=STAMP + 999))

    assert level_times(client)["Ayla"] == [{"level": 5, "seconds": 3600}]


def test_wall_clock_gaps_do_not_inflate_a_level(build_server):
    """The two samples are eight hours apart in real time but only one hour of
    played time -- the player slept. The level cost an hour."""
    _, client = build_server()
    accepted(client, status("Ayla", 5, 3600, at=STAMP))
    accepted(client, status("Ayla", 6, 7200, at=STAMP + 8 * 3600))

    assert level_times(client)["Ayla"][0]["seconds"] == 3600


def test_the_level_still_in_progress_is_not_reported(build_server):
    """Its cost isn't known until the character leaves it."""
    _, client = build_server()
    accepted(client, status("Ayla", 5, 3600, at=STAMP))
    accepted(client, status("Ayla", 6, 7200, at=STAMP + 100))

    assert [e["level"] for e in level_times(client)["Ayla"]] == [5]


def test_the_first_sample_at_a_level_is_the_boundary(build_server):
    """Mid-level check-ins arrive too; the level began at the first one."""
    _, client = build_server()
    accepted(client, status("Ayla", 5, 3600, at=STAMP))
    accepted(client, status("Ayla", 5, 5000, at=STAMP + 50))   # mid-level
    accepted(client, status("Ayla", 5, 6000, at=STAMP + 80))   # mid-level
    accepted(client, status("Ayla", 6, 7200, at=STAMP + 120))

    assert level_times(client)["Ayla"][0]["seconds"] == 3600


def test_characters_are_kept_apart(build_server):
    _, client = build_server()
    accepted(client, status("Ayla", 5, 3600, at=STAMP))
    accepted(client, status("Bo", 5, 100, at=STAMP + 1))
    accepted(client, status("Ayla", 6, 7200, at=STAMP + 2))
    accepted(client, status("Bo", 6, 200, at=STAMP + 3))

    times = level_times(client)
    assert times["Ayla"][0]["seconds"] == 3600
    assert times["Bo"][0]["seconds"] == 100


def test_a_skipped_level_is_not_bridged(build_server):
    """Without a sample at level 6, the cost of 5 is unknown -- reporting the
    5->7 gap as level 5 would silently double it."""
    _, client = build_server()
    accepted(client, status("Ayla", 5, 3600, at=STAMP))
    accepted(client, status("Ayla", 7, 9000, at=STAMP + 100))

    assert "Ayla" not in level_times(client)


def test_a_backwards_played_total_is_dropped(build_server):
    """SavedVariables resets and re-rolls can produce a lower total; a negative
    level cost is nonsense and would break the axis."""
    _, client = build_server()
    accepted(client, status("Ayla", 5, 9000, at=STAMP))
    accepted(client, status("Ayla", 6, 1000, at=STAMP + 100))

    assert "Ayla" not in level_times(client)


def test_a_character_with_no_status_rows_is_absent(build_server):
    _, client = build_server()
    accepted(client, f"Ghost,ZONE,{STAMP},Ironforge")

    assert "Ghost" not in level_times(client)
