"""Hiding a character from every public view.

Anyone can name a character anything, and that name reaches a public
dashboard the moment their watcher syncs. The operator needs to take one down
without waiting on the player -- and "down" has to mean every public view at
once, including the live feed, or it is not down.
"""
import threading

import pytest
from conftest import ROSTER_AUTH, accepted

STAMP = 1790000000


def receive_within(socket, seconds=5):
    """receive_json with a deadline, so a missing broadcast fails instead of hanging."""
    box = {}

    def receive():
        try:
            box["message"] = socket.receive_json()
        except Exception as exc:          # the socket closing is also an answer
            box["error"] = exc

    worker = threading.Thread(target=receive, daemon=True)
    worker.start()
    worker.join(seconds)
    if "message" not in box:
        pytest.fail(f"no message within {seconds}s")
    return box["message"]


def play(client, name, level=10, zone="Duskwood"):
    """A character with the kinds of history every analytics panel reads."""
    accepted(client, f"{name},PROFILE,{STAMP},Alliance,WARRIOR,")
    accepted(client, f"{name},ZONE,{STAMP},{zone}")
    accepted(client, f"{name},XP,{STAMP},{level},100,1000")
    accepted(client, f"{name},XP,{STAMP + 3600},{level + 1},50,1100")
    accepted(client, f"{name},QUEST,{STAMP + 60},123,450")
    accepted(client, f"{name},DEATH,{STAMP + 120},{level},{zone}")


def hide(client, name):
    response = client.post(f"/api/roster/{name}/hide", auth=ROSTER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def unhide(client, name):
    response = client.post(f"/api/roster/{name}/unhide", auth=ROSTER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def leaderboard_names(client):
    return {p["name"] for p in client.get("/api/leaderboard").json()}


def analytics(client):
    response = client.get("/api/analytics", auth=ROSTER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


# --- what comes off ---------------------------------------------------------

def test_a_hidden_character_leaves_the_leaderboard(build_server):
    _, client = build_server()
    play(client, "Rude Name")
    play(client, "Ayla")
    hide(client, "Rude Name")
    assert leaderboard_names(client) == {"Ayla"}


def test_a_hidden_character_is_not_in_a_new_dashboards_first_load(build_server):
    """The socket's INIT message is what a freshly opened big screen shows."""
    _, client = build_server()
    play(client, "Rude Name")
    play(client, "Ayla")
    hide(client, "Rude Name")
    with client.websocket_connect("/ws/dashboard") as socket:
        init = receive_within(socket)
    assert set(init["data"]) == {"Ayla"}


def test_a_hidden_character_leaves_every_analytics_panel(build_server):
    """Analytics is shared with viewers by link, so it is public enough.
    Checked panel by panel: a single missed query would put the name back."""
    _, client = build_server()
    play(client, "Rude Name", zone="Westfall")
    play(client, "Ayla", zone="Duskwood")
    before = analytics(client)
    hide(client, "Rude Name")
    data = analytics(client)

    assert [p["name"] for p in data["players"]] == ["Ayla"]
    assert "Rude Name" not in data["velocity"]
    assert "Rude Name" not in data["level_times"]
    assert "Rude Name" not in data["activity"]
    assert [z["zone"] for z in data["deaths_by_zone"]] == ["Duskwood"]
    assert data["totals"]["deaths"] == before["totals"]["deaths"] - 1
    assert data["totals"]["characters"] == 1
    assert "Rude Name" not in str(data), "the name must appear nowhere in the payload"


def test_open_dashboards_drop_it_at_once(build_server):
    """It has to leave the big screen while people are looking at it."""
    _, client = build_server()
    play(client, "Rude Name")
    with client.websocket_connect("/ws/dashboard") as socket:
        receive_within(socket)                         # INIT
        hide(client, "Rude Name")
        message = receive_within(socket)
    assert message == {"event": "PLAYER_REMOVED", "player": "Rude Name"}


def test_a_hidden_character_that_keeps_playing_stays_off_the_live_feed(build_server):
    """The likely case: the player does not stop because they were hidden."""
    _, client = build_server()
    play(client, "Rude Name")
    play(client, "Ayla")
    hide(client, "Rude Name")
    with client.websocket_connect("/ws/dashboard") as socket:
        receive_within(socket)                         # INIT
        accepted(client, f"Rude Name,ZONE,{STAMP},Stormwind City")
        accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
        message = receive_within(socket)
    # The next thing on the feed is Ayla's update; the hidden one sent nothing.
    assert message["player"] == "Ayla"


# --- what stays --------------------------------------------------------------

def test_hiding_does_not_delete_anything(build_server):
    """Events keep being recorded while hidden, so unhiding restores the
    character as it is now -- not as it was when it was hidden."""
    _, client = build_server()
    play(client, "Ayla", level=10)
    hide(client, "Ayla")
    accepted(client, f"Ayla,XP,{STAMP + 7200},15,0,2000")
    unhide(client, "Ayla")

    (ayla,) = [p for p in client.get("/api/leaderboard").json() if p["name"] == "Ayla"]
    assert ayla["level"] == 15
    assert "Ayla" in {p["name"] for p in analytics(client)["players"]}


def test_unhiding_puts_it_back_on_open_dashboards(build_server):
    _, client = build_server()
    play(client, "Ayla")
    hide(client, "Ayla")
    with client.websocket_connect("/ws/dashboard") as socket:
        receive_within(socket)                         # INIT
        unhide(client, "Ayla")
        message = receive_within(socket)
    assert message["event"] == "PLAYER_UPDATE"
    assert message["player"] == "Ayla"


def test_hidden_survives_a_restart(build_server, tmp_path):
    db = str(tmp_path / "hidden.db")
    _, client = build_server(DB_FILE=db)
    play(client, "Rude Name")
    play(client, "Ayla")
    hide(client, "Rude Name")

    _, restarted = build_server(DB_FILE=db)
    assert leaderboard_names(restarted) == {"Ayla"}
    assert [p["name"] for p in restarted.get("/api/roster/hidden", auth=ROSTER_AUTH).json()["hidden"]] == ["Rude Name"]


def test_the_operator_can_list_hidden_characters(build_server):
    """The roster page's only way to see them: the public feed never will."""
    _, client = build_server()
    play(client, "Rude Name", level=12)
    hide(client, "Rude Name")
    (entry,) = client.get("/api/roster/hidden", auth=ROSTER_AUTH).json()["hidden"]
    assert entry["name"] == "Rude Name"
    assert entry["level"] == 13
    assert entry["hidden_at"]


def test_a_name_with_an_apostrophe_is_handled_safely(build_server):
    """Names go into the analytics filter; a quote in one must neither break
    the query nor escape it."""
    _, client = build_server()
    play(client, "O'Brien")
    play(client, "Ayla")
    hide(client, "O'Brien")
    assert [p["name"] for p in analytics(client)["players"]] == ["Ayla"]
    unhide(client, "O'Brien")
    assert {p["name"] for p in analytics(client)["players"]} == {"Ayla", "O'Brien"}


# --- who can do it -------------------------------------------------------------

def test_only_an_operator_can_hide(build_server):
    _, client = build_server()
    play(client, "Ayla")
    assert client.post("/api/roster/Ayla/hide").status_code == 401

    minted = client.post("/api/access-tokens", json={"label": "v@example.com"}, auth=ROSTER_AUTH).json()
    client.get(f"/access?k={minted['token']}", follow_redirects=False)
    assert client.post("/api/roster/Ayla/hide").status_code == 401
    assert client.get("/api/roster/hidden").status_code == 401
    assert "Ayla" in leaderboard_names(client)


def test_an_unknown_name_is_refused_not_silently_accepted(build_server):
    """A typo'd name would otherwise report success and hide nothing."""
    _, client = build_server()
    play(client, "Ayla")
    response = client.post("/api/roster/Alya/hide", auth=ROSTER_AUTH)
    assert response.status_code == 404
    assert "Ayla" in leaderboard_names(client)
