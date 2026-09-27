"""Players: the people behind the characters, stored and editable.

A character is given a player the first time it arrives with a watcher or
account id -- joining whoever owns that account, else that install, else a
new player named after it. After that only the operator moves it: rename,
move a character (or split it out), merge two players.
"""
import sqlite3

from conftest import ROSTER_AUTH

STAMP = 1790000000
PC_A, PC_B = "a" * 32, "b" * 32
ACC_1, ACC_2, ACC_3 = "1" * 16, "2" * 16, "3" * 16


def send(client, name, watcher_id=None, account_id=None):
    body = {"timestamp": "t", "data": f"{name},ZONE,{STAMP},Ironforge"}
    if watcher_id:
        body["watcher_id"] = watcher_id
    if account_id:
        body["account_id"] = account_id
    result = client.post("/api/log-update", json=body).json()
    assert result["status"] == "success", result


def people(client):
    response = client.get("/api/people", auth=ROSTER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


def groups(client):
    return sorted(p["characters"] for p in people(client)["people"])


def person_named(client, name):
    (person,) = [p for p in people(client)["people"] if name in p["characters"]]
    return person


def post(client, url, body):
    response = client.post(url, json=body, auth=ROSTER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()


# --- automatic assignment -----------------------------------------------------------

def test_a_new_player_is_named_after_their_first_character(build_server):
    _, client = build_server()
    send(client, "Main", PC_A, ACC_1)
    send(client, "Alt", PC_A, ACC_1)
    person = person_named(client, "Alt")
    assert person["name"] == "Main"
    assert person["characters"] == ["Alt", "Main"]
    assert {m["name"] for m in person["members"]} == {"Alt", "Main"}


def test_characters_without_ids_wait_in_unlinked(build_server):
    """Watchers older than the ids send neither; those characters are listed
    so the operator can place them by hand."""
    _, client = build_server()
    send(client, "Old Watcher")
    data = people(client)
    assert data["people"] == []
    assert [c["name"] for c in data["unlinked"]] == ["Old Watcher"]


# --- operator edits ---------------------------------------------------------------------

def test_rename(build_server):
    _, client = build_server()
    send(client, "Main", PC_A, ACC_1)
    person = person_named(client, "Main")
    post(client, f"/api/people/{person['id']}/rename", {"name": "  Jerry  "})
    assert person_named(client, "Main")["name"] == "Jerry"


def test_split_a_character_out(build_server):
    """Two people sharing one PC are grouped by the install. Splitting one
    out gives them a player of their own, named after that character."""
    _, client = build_server()
    send(client, "Alice Main", PC_A, ACC_1)
    send(client, "Bob Main", PC_A, ACC_2)          # same PC, own account
    assert groups(client) == [["Alice Main", "Bob Main"]]

    post(client, "/api/people/move", {"player": "Bob Main", "person_id": None})
    assert groups(client) == [["Alice Main"], ["Bob Main"]]
    assert person_named(client, "Bob Main")["name"] == "Bob Main"


def test_after_a_split_the_next_alt_follows_its_account(build_server):
    """The account is checked before the install, so once Bob is split out,
    Bob's next character -- on the shared PC, but Bob's own account -- joins
    Bob, not Alice. The split keeps paying off instead of being undone."""
    _, client = build_server()
    send(client, "Alice Main", PC_A, ACC_1)
    send(client, "Bob Main", PC_A, ACC_2)
    post(client, "/api/people/move", {"player": "Bob Main", "person_id": None})

    send(client, "Bob Alt", PC_A, ACC_2)
    assert groups(client) == [["Alice Main"], ["Bob Alt", "Bob Main"]]


def test_move_a_character_to_another_player(build_server):
    _, client = build_server()
    send(client, "Ayla", PC_A, ACC_1)
    send(client, "Laptop Char", PC_B, ACC_2)        # same person, different PC and account
    target = person_named(client, "Ayla")["id"]
    post(client, "/api/people/move", {"player": "Laptop Char", "person_id": target})
    assert groups(client) == [["Ayla", "Laptop Char"]]


def test_merge_two_players(build_server):
    module, client = build_server()
    send(client, "Ayla", PC_A, ACC_1)
    send(client, "Ayla Alt", PC_A, ACC_1)
    send(client, "Laptop Char", PC_B, ACC_2)
    keep = person_named(client, "Ayla")
    gone = person_named(client, "Laptop Char")
    post(client, "/api/people/merge", {"from_id": gone["id"], "into_id": keep["id"]})

    assert groups(client) == [["Ayla", "Ayla Alt", "Laptop Char"]]
    assert person_named(client, "Laptop Char")["name"] == keep["name"]
    # Checked in the database: the listing skips empty players anyway, so a
    # leftover row would never show there -- it would just pile up.
    with sqlite3.connect(module.DB_FILE) as db:
        assert db.execute("SELECT id FROM people").fetchall() == [(keep["id"],)]


def test_an_unlinked_character_can_be_placed_by_hand(build_server):
    _, client = build_server()
    send(client, "Ayla", PC_A, ACC_1)
    send(client, "Old Watcher")
    post(client, "/api/people/move", {"player": "Old Watcher", "person_id": person_named(client, "Ayla")["id"]})
    data = people(client)
    assert data["unlinked"] == []
    assert groups(client) == [["Ayla", "Old Watcher"]]


def test_a_moved_character_stays_moved(build_server):
    """Assigned once; later events from the same ids must not pull it back."""
    _, client = build_server()
    send(client, "Alice Main", PC_A, ACC_1)
    send(client, "Bob Main", PC_A, ACC_2)
    post(client, "/api/people/move", {"player": "Bob Main", "person_id": None})
    for _ in range(3):
        send(client, "Bob Main", PC_A, ACC_2)
    assert groups(client) == [["Alice Main"], ["Bob Main"]]


def test_bad_requests_are_refused(build_server):
    _, client = build_server()
    send(client, "Ayla", PC_A, ACC_1)
    pid = person_named(client, "Ayla")["id"]
    assert client.post("/api/people/move", json={"player": "Nobody", "person_id": pid},
                       auth=ROSTER_AUTH).status_code == 404
    assert client.post("/api/people/move", json={"player": "Ayla", "person_id": 999},
                       auth=ROSTER_AUTH).status_code == 404
    assert client.post(f"/api/people/{pid}/rename", json={"name": "   "}, auth=ROSTER_AUTH).status_code == 422
    assert client.post("/api/people/merge", json={"from_id": pid, "into_id": pid},
                       auth=ROSTER_AUTH).status_code == 422


def test_only_an_operator_can_see_or_edit_players(build_server):
    """Which characters are one person is not public: it would out an alt
    someone keeps separate on purpose."""
    _, client = build_server()
    send(client, "Ayla", PC_A, ACC_1)
    pid = person_named(client, "Ayla")["id"]
    assert client.get("/api/people").status_code == 401
    assert client.post(f"/api/people/{pid}/rename", json={"name": "x"}).status_code == 401
    assert client.post("/api/people/move", json={"player": "Ayla"}).status_code == 401
    assert client.post("/api/people/merge", json={"from_id": pid, "into_id": pid}).status_code == 401


# --- persistence ---------------------------------------------------------------------------

def test_edits_survive_a_restart(build_server, tmp_path):
    db = str(tmp_path / "players.db")
    _, client = build_server(DB_FILE=db)
    send(client, "Alice Main", PC_A, ACC_1)
    send(client, "Bob Main", PC_A, ACC_2)
    post(client, "/api/people/move", {"player": "Bob Main", "person_id": None})
    post(client, f"/api/people/{person_named(client, 'Alice Main')['id']}/rename", {"name": "Alice"})

    _, restarted = build_server(DB_FILE=db)
    assert groups(restarted) == [["Alice Main"], ["Bob Main"]]
    assert person_named(restarted, "Alice Main")["name"] == "Alice"
    send(restarted, "Bob Alt", PC_A, ACC_2)
    assert groups(restarted) == [["Alice Main"], ["Bob Alt", "Bob Main"]]


def test_links_from_before_players_existed_are_grouped_on_first_start(build_server, tmp_path):
    """Production already has character_sources rows from before this
    feature. On the first start with it, those characters must come out
    grouped by the same rule -- not one player per character, and not lost."""
    db = str(tmp_path / "upgrade.db")
    _, client = build_server(DB_FILE=db)
    send(client, "Main", PC_A, ACC_1)
    send(client, "Alt", PC_B, ACC_1)
    send(client, "Stranger", PC_B, ACC_3)
    with sqlite3.connect(db) as conn:                   # back to how it looked before
        conn.execute("DELETE FROM character_person")
        conn.execute("DELETE FROM people")

    _, upgraded = build_server(DB_FILE=db)
    assert groups(upgraded) == [["Alt", "Main", "Stranger"]]
    assert person_named(upgraded, "Main")["name"] == "Main", "named after the oldest character"


def tags(client, name):
    (player,) = [p for p in client.get("/api/leaderboard").json() if p["name"] == name]
    return player["tags"]


def test_splitting_a_character_out_drops_its_automatic_alt_tag(build_server):
    """Bob was tagged "alt" only because he was grouped with Alice on a
    shared PC. On his own he is his player's main, so the tag goes."""
    _, client = build_server()
    send(client, "Alice Main", PC_A, ACC_1)
    send(client, "Bob Main", PC_A, ACC_2)
    assert tags(client, "Bob Main") == ["alt"]

    post(client, "/api/people/move", {"player": "Bob Main", "person_id": None})
    assert tags(client, "Bob Main") == []


def test_moving_into_another_player_leaves_tags_alone(build_server):
    """Joining someone is not the same as becoming their main or their alt;
    whether to tag it is the operator's call."""
    _, client = build_server()
    send(client, "Ayla", PC_A, ACC_1)
    send(client, "Ayla Alt", PC_A, ACC_1)
    send(client, "Laptop Char", PC_B, ACC_2)
    post(client, "/api/people/move", {"player": "Ayla Alt", "person_id": person_named(client, "Laptop Char")["id"]})
    assert tags(client, "Ayla Alt") == ["alt"]
    assert tags(client, "Laptop Char") == []
