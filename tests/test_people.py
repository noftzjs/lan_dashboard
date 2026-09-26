"""Linking characters to the people playing them.

Every watcher sends two opaque ids with each event: a random one generated on
first run (one per install) and a hash of the WoW account folder (one per
account, shared by every character on it). The server records which ids each
character has been seen with and groups characters into people from that.
"""
from conftest import ROSTER_AUTH

STAMP = 1790000000
PC_A, PC_B = "a" * 32, "b" * 32
ACCOUNT_1, ACCOUNT_2 = "1" * 16, "2" * 16


def send(client, data, watcher_id=None, account_id=None):
    body = {"timestamp": "2026-09-26T12:00:00", "data": data}
    if watcher_id is not None:
        body["watcher_id"] = watcher_id
    if account_id is not None:
        body["account_id"] = account_id
    return client.post("/api/log-update", json=body).json()


def zone(client, name, **ids):
    result = send(client, f"{name},ZONE,{STAMP},Ironforge", **ids)
    assert result["status"] == "success", result
    return result


def people(client):
    response = client.get("/api/people", auth=ROSTER_AUTH)
    assert response.status_code == 200, response.text
    return response.json()["people"]


def groups(client):
    """Just the character groupings, order-independent."""
    return sorted(p["characters"] for p in people(client))


def test_characters_on_one_account_are_one_person(build_server):
    """The account folder is shared by every character on it, and survives a
    watcher reinstall -- the strongest link available."""
    _, client = build_server()
    zone(client, "Main", watcher_id=PC_A, account_id=ACCOUNT_1)
    zone(client, "Alt", watcher_id=PC_B, account_id=ACCOUNT_1)
    assert groups(client) == [["Alt", "Main"]]


def test_characters_from_one_install_are_one_person(build_server):
    _, client = build_server()
    zone(client, "Main", watcher_id=PC_A, account_id=ACCOUNT_1)
    zone(client, "Other Account Char", watcher_id=PC_A, account_id=ACCOUNT_2)
    assert groups(client) == [["Main", "Other Account Char"]]


def test_links_chain(build_server):
    """Alt shares an account with Main; Main shares a PC with Third. All one
    person, though Alt and Third share nothing directly."""
    _, client = build_server()
    zone(client, "Alt", watcher_id=PC_B, account_id=ACCOUNT_1)
    zone(client, "Main", watcher_id=PC_A, account_id=ACCOUNT_1)
    zone(client, "Third", watcher_id=PC_A, account_id=ACCOUNT_2)
    assert groups(client) == [["Alt", "Main", "Third"]]


def test_unrelated_characters_stay_apart(build_server):
    _, client = build_server()
    zone(client, "Ayla", watcher_id=PC_A, account_id=ACCOUNT_1)
    zone(client, "Bo", watcher_id=PC_B, account_id=ACCOUNT_2)
    assert groups(client) == [["Ayla"], ["Bo"]]


def test_a_person_lists_every_id_and_counts_events(build_server):
    _, client = build_server()
    zone(client, "Main", watcher_id=PC_A, account_id=ACCOUNT_1)
    zone(client, "Main", watcher_id=PC_A, account_id=ACCOUNT_1)
    zone(client, "Alt", watcher_id=PC_B, account_id=ACCOUNT_1)

    (person,) = people(client)
    assert person["watcher_ids"] == [PC_A, PC_B]
    assert person["account_ids"] == [ACCOUNT_1]
    assert person["events"] == 3
    assert person["first_seen"] <= person["last_seen"]


def test_an_old_watcher_sending_no_ids_still_works(build_server):
    """Watchers built before this send neither id. Their events must be
    accepted exactly as before; there is just nothing to link on."""
    _, client = build_server()
    zone(client, "Ayla")
    assert people(client) == []
    names = [p["name"] for p in client.get("/api/leaderboard").json()]
    assert "Ayla" in names


def test_one_id_is_enough(build_server):
    _, client = build_server()
    zone(client, "Main", account_id=ACCOUNT_1)
    zone(client, "Alt", account_id=ACCOUNT_1)
    zone(client, "Solo", watcher_id=PC_A)
    assert groups(client) == [["Alt", "Main"], ["Solo"]]


def test_a_garbled_id_is_ignored_not_fatal(build_server):
    """Losing someone's level-up over a bad id would be the wrong trade."""
    _, client = build_server()
    zone(client, "Ayla", watcher_id="not hex!", account_id="x" * 16)
    zone(client, "Bo", watcher_id="<script>", account_id=ACCOUNT_1)
    (person,) = people(client)
    assert person["characters"] == ["Bo"]
    assert person["watcher_ids"] == []


def test_ids_are_normalised_to_lowercase(build_server):
    _, client = build_server()
    zone(client, "Main", account_id="ABCDEF0123456789")
    zone(client, "Alt", account_id="abcdef0123456789")
    assert groups(client) == [["Alt", "Main"]]


def test_a_refused_event_does_not_link(build_server):
    """A refused payload can name any character. Linking on it would let a
    typo -- or anyone with the URL -- attach a character to a person."""
    _, client = build_server()
    zone(client, "Main", watcher_id=PC_A)
    result = send(client, f"Stranger,STATUS,{STAMP},99,0,0,0,0,0", watcher_id=PC_A)
    assert result["status"] == "error"
    assert groups(client) == [["Main"]]


def test_the_links_survive_a_restart(build_server, tmp_path):
    db = str(tmp_path / "people.db")
    _, client = build_server(DB_FILE=db)
    zone(client, "Main", watcher_id=PC_A, account_id=ACCOUNT_1)
    zone(client, "Alt", account_id=ACCOUNT_1)

    _, restarted = build_server(DB_FILE=db)
    assert groups(restarted) == [["Alt", "Main"]]


def test_only_an_operator_can_see_people(build_server):
    """Which characters belong to one person is not public: it would out an
    alt someone keeps separate on purpose."""
    _, client = build_server()
    zone(client, "Main", watcher_id=PC_A)
    assert client.get("/api/people").status_code == 401

    minted = client.post("/api/access-tokens", json={"label": "v@example.com"}, auth=ROSTER_AUTH).json()
    client.get(f"/access?k={minted['token']}", follow_redirects=False)
    assert client.get("/api/people").status_code == 401


def test_the_traffic_log_shows_which_install_sent_an_event(build_server):
    _, client = build_server()
    zone(client, "Main", watcher_id=PC_A, account_id=ACCOUNT_1)
    send(client, f"Main,STATUS,{STAMP},99,0,0,0,0,0", watcher_id=PC_B)

    entries = client.get("/api/traffic", auth=ROSTER_AUTH).json()["entries"]
    assert (entries[-2]["watcher_id"], entries[-2]["account_id"]) == (PC_A, ACCOUNT_1)
    assert (entries[-1]["watcher_id"], entries[-1]["accepted"]) == (PC_B, False)


def test_a_wrong_token_still_shows_which_install_sent_it(build_server):
    """The case this exists for: "whose watcher still has the old token?"."""
    _, client = build_server(INGESTION_TOKEN="s3cret")
    response = client.post("/api/log-update", headers={"X-Ingestion-Token": "old"},
                           json={"timestamp": "t", "data": f"Main,ZONE,{STAMP},X", "watcher_id": PC_A})
    assert response.status_code == 401

    entry = client.get("/api/traffic", auth=ROSTER_AUTH).json()["entries"][-1]
    assert entry["watcher_id"] == PC_A
    assert entry["accepted"] is False
