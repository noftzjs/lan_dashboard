"""The automatic "alt" tag.

The first character seen from a watcher install or a WoW account is that
person's main and gets no tag. Any other character that later arrives from
the same install or account gets "alt" -- once. The operator can remove or
change it on the roster page, and that decision stands.
"""
import sqlite3

from conftest import ROSTER_AUTH

STAMP = 1790000000
PC_A, PC_B = "a" * 32, "b" * 32
ACCOUNT_1, ACCOUNT_2 = "1" * 16, "2" * 16


def send(client, name, watcher_id=None, account_id=None, data=None):
    body = {"timestamp": "t", "data": data or f"{name},ZONE,{STAMP},Ironforge"}
    if watcher_id:
        body["watcher_id"] = watcher_id
    if account_id:
        body["account_id"] = account_id
    return client.post("/api/log-update", json=body)


def tags(client, name):
    (player,) = [p for p in client.get("/api/leaderboard").json() if p["name"] == name]
    return player["tags"]


def test_the_first_character_is_the_main_and_gets_no_tag(build_server):
    _, client = build_server()
    send(client, "Main", PC_A, ACCOUNT_1)
    assert tags(client, "Main") == []


def test_a_second_character_from_the_same_install_is_an_alt(build_server):
    _, client = build_server()
    send(client, "Main", PC_A, ACCOUNT_1)
    send(client, "Bank Alt", PC_A, ACCOUNT_2)
    assert tags(client, "Bank Alt") == ["alt"]
    assert tags(client, "Main") == [], "the main is never retagged"


def test_a_second_character_on_the_same_account_is_an_alt(build_server):
    """Same account from another PC: the account folder is what links them."""
    _, client = build_server()
    send(client, "Main", PC_A, ACCOUNT_1)
    send(client, "Laptop Alt", PC_B, ACCOUNT_1)
    assert tags(client, "Laptop Alt") == ["alt"]


def test_unrelated_characters_get_no_tag(build_server):
    _, client = build_server()
    send(client, "Ayla", PC_A, ACCOUNT_1)
    send(client, "Bo", PC_B, ACCOUNT_2)
    assert tags(client, "Bo") == []


def test_removing_the_tag_sticks(build_server):
    """Decided once, when the character first appears. If the operator says
    it is not an alt, later events must not put the tag back."""
    _, client = build_server()
    send(client, "Main", PC_A, ACCOUNT_1)
    send(client, "Second Main", PC_A, ACCOUNT_1)
    assert tags(client, "Second Main") == ["alt"]

    response = client.post("/api/roster/Second Main", json={"tags": []}, auth=ROSTER_AUTH)
    assert response.status_code == 200
    for _ in range(3):
        send(client, "Second Main", PC_A, ACCOUNT_1)
    assert tags(client, "Second Main") == []


def test_existing_tags_are_kept_and_a_full_set_is_left_alone(build_server):
    _, client = build_server()
    send(client, "Main", PC_A, ACCOUNT_1)
    client.post("/api/roster/Tagged", json={"tags": ["Healer"]}, auth=ROSTER_AUTH)
    client.post("/api/roster/Full", json={"tags": ["One", "Two", "Three"]}, auth=ROSTER_AUTH)

    send(client, "Tagged", PC_A, ACCOUNT_1)
    send(client, "Full", PC_A, ACCOUNT_1)
    assert tags(client, "Tagged") == ["Healer", "alt"]
    assert tags(client, "Full") == ["One", "Two", "Three"], "never pushes out a tag the operator set"


def test_an_old_watcher_sending_no_ids_tags_nothing(build_server):
    _, client = build_server()
    send(client, "Main")
    send(client, "Other")
    assert tags(client, "Other") == []


def test_a_refused_event_does_not_decide(build_server):
    """A refused payload can name any character; it must not tag one. The
    character's first accepted event is what counts."""
    _, client = build_server()
    send(client, "Main", PC_A, ACCOUNT_1)
    refused = send(client, "Alt", PC_A, ACCOUNT_1, data=f"Alt,STATUS,{STAMP},99,0,0,0,0,0").json()
    assert refused["status"] == "error"
    send(client, "Alt", PC_A, ACCOUNT_1)
    assert tags(client, "Alt") == ["alt"]


def test_a_failed_save_leaves_no_tag_and_the_retry_decides(build_server, monkeypatch):
    """The tag is applied only once the event is saved. A busy database
    answers "retry", and the retried event must still get its tag."""
    module, client = build_server()
    send(client, "Main", PC_A, ACCOUNT_1)
    real_apply = module._apply_writes

    def locked(_statements):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(module, "_apply_writes", locked)
    assert send(client, "Alt", PC_A, ACCOUNT_1).status_code == 503
    monkeypatch.setattr(module, "_apply_writes", real_apply)

    send(client, "Alt", PC_A, ACCOUNT_1)
    assert tags(client, "Alt") == ["alt"]


def test_tags_and_links_survive_a_restart(build_server, tmp_path):
    """After a restart the server must still know who came from where, or
    the next new character from a known PC would be taken for a main."""
    db = str(tmp_path / "alts.db")
    _, client = build_server(DB_FILE=db)
    send(client, "Main", PC_A, ACCOUNT_1)
    send(client, "Alt", PC_A, ACCOUNT_1)

    _, restarted = build_server(DB_FILE=db)
    assert tags(restarted, "Alt") == ["alt"]
    send(restarted, "Third", PC_A, ACCOUNT_2)
    assert tags(restarted, "Third") == ["alt"]
