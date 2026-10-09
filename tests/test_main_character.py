"""A player's main, and alts that say whose alt they are.

The main defaults to the character the player was created from. "Make main"
on the Players tab moves it: the new main loses its "alt" tag and every other
character of the player gets one. On the dashboard, hovering an "alt" tag
says "Alt of <main>" -- the only public trace of the grouping, and only on
characters already tagged alt.
"""
import sqlite3
import threading

import pytest
from conftest import ROSTER_AUTH

STAMP = 1790000000
PC_A, PC_B = "a" * 32, "b" * 32
ACC_1, ACC_2 = "1" * 16, "2" * 16


def send(client, name, watcher_id=PC_A, account_id=ACC_1):
    body = {"timestamp": "t", "data": f"{name},ZONE,{STAMP},Ironforge"}
    if watcher_id:
        body["watcher_id"] = watcher_id
    if account_id:
        body["account_id"] = account_id
    assert client.post("/api/log-update", json=body).json()["status"] == "success"


def board(client):
    return {p["name"]: p for p in client.get("/api/leaderboard").json()}


def person_id(client, name):
    (person,) = [p for p in client.get("/api/people", auth=ROSTER_AUTH).json()["people"] if name in p["characters"]]
    return person["id"]


def make_main(client, name):
    response = client.post(f"/api/people/{person_id(client, name)}/main", json={"player": name}, auth=ROSTER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def receive_within(socket, seconds=5):
    box = {}

    def receive():
        try:
            box["message"] = socket.receive_json()
        except Exception as exc:          # noqa: BLE001 -- the socket closing is also an answer
            box["error"] = exc

    worker = threading.Thread(target=receive, daemon=True)
    worker.start()
    worker.join(seconds)
    if "message" not in box:
        pytest.fail(f"no message within {seconds}s")
    return box["message"]


# --- the default main and the hover -------------------------------------------------------

def test_an_alt_says_whose_alt_it_is(build_server):
    _, client = build_server()
    send(client, "Main Char")
    send(client, "Bank Alt")
    players = board(client)
    assert players["Bank Alt"]["tags"] == ["alt"]
    assert players["Bank Alt"]["alt_of"] == "Main Char", "the first character is the assumed main"
    assert players["Main Char"]["alt_of"] is None


def test_only_characters_tagged_alt_say_anything(build_server):
    """The tag is what's public; the hover only explains it. A grouped
    character the operator untagged must not reveal who it belongs to."""
    _, client = build_server()
    send(client, "Main Char")
    send(client, "Quiet One")
    client.post("/api/roster/Quiet One", json={"tags": []}, auth=ROSTER_AUTH)
    assert board(client)["Quiet One"]["alt_of"] is None


def test_a_hidden_main_is_never_named(build_server):
    _, client = build_server()
    send(client, "Main Char")
    send(client, "Bank Alt")
    client.post("/api/roster/Main Char/hide", auth=ROSTER_AUTH)
    assert board(client)["Bank Alt"]["alt_of"] is None


def test_a_character_in_no_player_names_nobody(build_server):
    """An old watcher sends no ids, so there is no player to be an alt of --
    even if the operator tags it by hand."""
    _, client = build_server()
    send(client, "Old Watcher", watcher_id=None, account_id=None)
    client.post("/api/roster/Old Watcher", json={"tags": ["alt"]}, auth=ROSTER_AUTH)
    assert board(client)["Old Watcher"]["alt_of"] is None


# --- make main ------------------------------------------------------------------------------

def test_make_main_swaps_the_tags(build_server):
    _, client = build_server()
    send(client, "First Char")
    send(client, "Real Main")
    send(client, "Bank Alt")
    make_main(client, "Real Main")

    players = board(client)
    assert players["Real Main"]["tags"] == []
    assert players["First Char"]["tags"] == ["alt"]
    assert players["Bank Alt"]["tags"] == ["alt"]
    assert players["First Char"]["alt_of"] == "Real Main"
    assert players["Bank Alt"]["alt_of"] == "Real Main"


def test_make_main_keeps_other_tags(build_server):
    _, client = build_server()
    send(client, "First Char")
    send(client, "Real Main")
    client.post("/api/roster/Real Main", json={"tags": ["Healer", "alt"]}, auth=ROSTER_AUTH)
    client.post("/api/roster/First Char", json={"tags": ["Tank"]}, auth=ROSTER_AUTH)
    make_main(client, "Real Main")
    players = board(client)
    assert players["Real Main"]["tags"] == ["Healer"]
    assert players["First Char"]["tags"] == ["Tank", "alt"]


def test_a_full_tag_set_is_left_alone_and_reported(build_server):
    """Never pushes out a tag the operator chose; says which it skipped."""
    _, client = build_server()
    send(client, "First Char")
    send(client, "Real Main")
    client.post("/api/roster/First Char", json={"tags": ["One", "Two", "Three"]}, auth=ROSTER_AUTH)
    result = make_main(client, "Real Main")
    assert result["skipped"] == ["First Char"]
    assert board(client)["First Char"]["tags"] == ["One", "Two", "Three"]


def test_open_dashboards_get_the_new_hover(build_server):
    _, client = build_server()
    send(client, "First Char")
    send(client, "Real Main")
    send(client, "Bank Alt")
    with client.websocket_connect("/ws/dashboard") as socket:
        receive_within(socket)                         # INIT
        make_main(client, "Real Main")
        updates = {}
        for _ in range(3):
            message = receive_within(socket)
            updates[message["player"]] = message["state"]
    assert updates["Bank Alt"]["alt_of"] == "Real Main"
    assert updates["Real Main"]["tags"] == []


def test_make_main_refuses_a_character_from_another_player(build_server):
    _, client = build_server()
    send(client, "Ayla")
    send(client, "Stranger", watcher_id=PC_B, account_id=ACC_2)
    response = client.post(f"/api/people/{person_id(client, 'Ayla')}/main", json={"player": "Stranger"},
                           auth=ROSTER_AUTH)
    assert response.status_code == 422
    assert client.post("/api/people/999/main", json={"player": "Ayla"}, auth=ROSTER_AUTH).status_code == 404


def test_only_an_operator_can_make_a_main(build_server):
    _, client = build_server()
    send(client, "Ayla")
    assert client.post(f"/api/people/{person_id(client, 'Ayla')}/main", json={"player": "Ayla"}).status_code == 401


# --- when the main moves away, and persistence -------------------------------------------------

def test_when_the_main_is_moved_out_the_remaining_untagged_character_is_assumed(build_server):
    _, client = build_server()
    send(client, "First Char")
    send(client, "Bank Alt")
    send(client, "Other Main")
    make_main(client, "Other Main")                     # First Char and Bank Alt now alts
    client.post("/api/roster/First Char", json={"tags": []}, auth=ROSTER_AUTH)
    client.post("/api/people/move", json={"player": "Other Main", "person_id": None}, auth=ROSTER_AUTH)
    assert board(client)["Bank Alt"]["alt_of"] == "First Char"


def test_the_main_survives_a_restart(build_server, tmp_path):
    db = str(tmp_path / "main.db")
    _, client = build_server(DB_FILE=db)
    send(client, "First Char")
    send(client, "Real Main")
    make_main(client, "Real Main")

    _, restarted = build_server(DB_FILE=db)
    assert board(restarted)["First Char"]["alt_of"] == "Real Main"


def test_players_from_before_mains_existed_get_their_founder(build_server, tmp_path):
    """Production has players created before the main column: each gets the
    character it was named after, if that character is still theirs."""
    db = str(tmp_path / "upgrade.db")
    _, client = build_server(DB_FILE=db)
    send(client, "First Char")
    send(client, "Bank Alt")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE people SET main_player = NULL")

    _, upgraded = build_server(DB_FILE=db)
    assert board(upgraded)["Bank Alt"]["alt_of"] == "First Char"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT main_player FROM people").fetchall() == [("First Char",)]
