"""Operator-curated metadata (tags, stream links) and the analytics
aggregation — the parts where a number being quietly wrong matters more than
a request failing."""
import pytest
from conftest import ROSTER_AUTH, accepted, leaderboard

STAMP = 1790000000


def set_meta(client, player, **body):
    response = client.post(f"/api/roster/{player}", json=body, auth=ROSTER_AUTH)
    assert response.status_code == 200
    return response.json()["state"]


# --- tags --------------------------------------------------------------------

def test_tags_are_capped_at_three(server):
    state = set_meta(server, "Ayla", tags=["one", "two", "three", "four", "five"])
    assert state["tags"] == ["one", "two", "three"]


def test_tag_text_is_trimmed_and_length_capped(server):
    state = set_meta(server, "Ayla", tags=["  spaced  ", "x" * 60, "   "])
    assert state["tags"][0] == "spaced"
    assert len(state["tags"][1]) == 30
    assert len(state["tags"]) == 2          # the blank one is dropped


# --- stream links ------------------------------------------------------------

def test_multiple_stream_links_are_kept_in_order_and_capped(server):
    state = set_meta(server, "Ayla", stream_urls=[
        "https://twitch.tv/a", "https://kick.com/b", "https://youtube.com/@c", "https://twitch.tv/d"])
    assert state["stream_urls"] == ["https://twitch.tv/a", "https://kick.com/b", "https://youtube.com/@c"]


def test_a_legacy_single_link_client_still_works(server):
    """An older browser tab posts stream_url; it becomes a one-item list."""
    state = set_meta(server, "Ayla", stream_url="https://twitch.tv/legacy")
    assert state["stream_urls"] == ["https://twitch.tv/legacy"]


def test_stream_urls_wins_when_a_client_sends_both(server):
    state = set_meta(server, "Ayla",
                     stream_url="https://twitch.tv/old",
                     stream_urls=["https://kick.com/new"])
    assert state["stream_urls"] == ["https://kick.com/new"]


def test_links_can_be_cleared(server):
    set_meta(server, "Ayla", stream_urls=["https://twitch.tv/a"])
    assert set_meta(server, "Ayla", stream_urls=[])["stream_urls"] == []


def test_roster_metadata_survives_a_restart(build_server):
    """It lives only in the roster table, so a reload has to rebuild it."""
    module, client = build_server()
    set_meta(client, "Ayla", tags=["Team Alpha"], stream_urls=["https://twitch.tv/a"])

    import asyncio
    module.player_states.clear()
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        module.reload_states_from_db())
    restored = module.player_states["Ayla"]
    assert restored["tags"] == ["Team Alpha"]
    assert restored["stream_urls"] == ["https://twitch.tv/a"]


# --- staleness ---------------------------------------------------------------

def test_a_character_the_server_has_never_heard_from_reports_no_age(server):
    """idle_seconds of None is what the client reads as "stale, hide it"."""
    state = set_meta(server, "NeverPlayed", tags=["x"])
    assert state["idle_seconds"] is None


def test_activity_refreshes_the_age(server):
    accepted(server, f"Active,XP,{STAMP},10,100,1000")
    assert leaderboard(server)["Active"]["idle_seconds"] == pytest.approx(0, abs=5)


# --- analytics aggregation ---------------------------------------------------

def analytics(client):
    response = client.get("/api/analytics", auth=ROSTER_AUTH)
    assert response.status_code == 200
    return response.json()


def test_xp_totals_count_the_xp_that_completed_each_level(server):
    """Summing only within-level deltas undercounts: finishing level 10 at
    7,600 and starting level 11 is 7,600 of real progress that a naive diff
    throws away because the counter reset."""
    accepted(server, f"Climber,XP,{STAMP},10,7000,7600")
    accepted(server, f"Climber,XP,{STAMP + 60},10,7500,7600")     # +500 within the level
    accepted(server, f"Climber,XP,{STAMP + 120},11,300,8800")     # finished 10 (+100) then +300

    player = next(p for p in analytics(server)["players"] if p["name"] == "Climber")
    assert player["total_xp"] == 900


def test_levels_gained_counts_only_what_was_observed(server):
    """A character first seen at 10 and now 12 gained 2 here — not 11. The
    dashboard must not take credit for levels earned before it existed."""
    accepted(server, f"Late,XP,{STAMP},10,100,7600")
    accepted(server, f"Late,XP,{STAMP + 60},12,200,10100")
    assert analytics(server)["totals"]["levels_gained"] == 2


def test_quest_xp_and_other_xp_split(server):
    accepted(server, f"Mixed,XP,{STAMP},10,0,7600")
    accepted(server, f"Mixed,XP,{STAMP + 60},10,1000,7600")       # 1000 total gained
    accepted(server, f"Mixed,QUEST,{STAMP + 30},1,400")           # 400 of it from a quest
    player = next(p for p in analytics(server)["players"] if p["name"] == "Mixed")
    assert (player["quest_xp"], player["other_xp"], player["total_xp"]) == (400, 600, 1000)


def test_other_xp_never_goes_negative(server):
    """Quest XP is reported by the game while the total is inferred from
    snapshots, so a quest arriving without its XP tick must not underflow."""
    accepted(server, f"Odd,QUEST,{STAMP},1,5000")
    player = next(p for p in analytics(server)["players"] if p["name"] == "Odd")
    assert player["other_xp"] == 0


def test_quest_bands_add_up_to_the_whole(server):
    for index, reward in enumerate([100, 400, 800, 1500, 3000]):
        accepted(server, f"Bander,QUEST,{STAMP + index},{index},{reward}")
    data = analytics(server)
    assert data["totals"]["quest_xp"] == 5800
    assert sum(band["count"] for band in data["quest_bands"]) == 5
    assert sum(band["share"] for band in data["quest_bands"]) == pytest.approx(100, abs=0.5)


def test_deaths_are_reported_per_player_and_in_total(server):
    accepted(server, f"Dier,DEATH,{STAMP},19,Deadmines")
    accepted(server, f"Dier,DEATH,{STAMP + 60},19,Duskwood")
    accepted(server, f"Survivor,XP,{STAMP},10,100,7600")
    data = analytics(server)
    by_name = {p["name"]: p for p in data["players"]}
    assert by_name["Dier"]["deaths"] == 2
    assert by_name["Survivor"]["deaths"] == 0
    assert data["totals"]["deaths"] == 2


def test_velocity_needs_two_separate_moments_to_draw_a_line(server):
    accepted(server, f"OnePoint,XP,{STAMP},10,100,7600")
    accepted(server, f"TwoPoints,XP,{STAMP},10,100,7600")
    accepted(server, f"TwoPoints,XP,{STAMP + 3600},11,200,8800")
    velocity = analytics(server)["velocity"]
    assert "OnePoint" not in velocity
    assert len(velocity["TwoPoints"]) == 2


def test_untimed_history_produces_no_velocity_lines(server):
    """Everything recorded before addon v2.11.0 has no in-game clock."""
    accepted(server, "Old,XP,10,100,7600")
    accepted(server, "Old,XP,11,200,8800")
    data = analytics(server)
    assert data["velocity"] == {}
    assert data["history"]["timed_rows"] == 0


def test_a_capped_character_is_visible_to_the_analytics_page(server):
    accepted(server, f"Capped,XP,{STAMP},20,0,0")
    player = next(p for p in analytics(server)["players"] if p["name"] == "Capped")
    assert player["max_xp"] == 0          # what the page keys off to say "MAX"
    assert player["level"] == 20
