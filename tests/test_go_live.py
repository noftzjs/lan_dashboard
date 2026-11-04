"""The go-live cutoff, and activity measured by when an event happened.

Beta testers' addons still hold weeks of beta events, and an updated or
reinstalled watcher sends the whole queue again. Before this, that put old
beta characters back on the live board and made them look active, because
last_activity was stamped with the arrival time. GO_LIVE_AT refuses those
events before anything is written.
"""
import sqlite3
import time
from datetime import datetime, timezone

import pytest
from conftest import ROSTER_AUTH, accepted, leaderboard, post_event

GO_LIVE = "2026-11-10T18:00:00-05:00"
CUTOFF = int(datetime.fromisoformat(GO_LIVE).timestamp())     # 23:00 UTC


def ignored(client, data):
    body = post_event(client, data).json()
    assert body["status"] == "ignored", f"expected {data!r} to be ignored, got {body}"
    return body["detail"]


# --- the cutoff ---------------------------------------------------------------

def test_an_event_from_before_go_live_is_ignored(build_server):
    module, client = build_server(GO_LIVE_AT=GO_LIVE)
    ignored(client, f"Beta,XP,{CUTOFF - 1},20,100,1000")
    assert "Beta" not in module.player_states
    assert "Beta" not in leaderboard(client)


def test_an_event_from_go_live_onward_is_accepted(build_server):
    _module, client = build_server(GO_LIVE_AT=GO_LIVE)
    accepted(client, f"Live,XP,{CUTOFF},2,100,1000")
    accepted(client, f"Live,XP,{CUTOFF + 60},3,100,1000")
    assert leaderboard(client)["Live"]["level"] == 3


def test_an_ignored_event_writes_nothing(build_server):
    """Not even the people/source records: a beta character must not be
    linked to an install or tagged as an alt by a refused event."""
    module, client = build_server(GO_LIVE_AT=GO_LIVE)
    client.post("/api/log-update", json={
        "timestamp": "2026-11-10T12:00:00", "data": f"Beta,ZONE,{CUTOFF - 3600},Elwynn Forest",
        "watcher_id": "aaaa1111", "account_id": "ACCOUNT"})
    connection = sqlite3.connect(module.DB_FILE)
    try:
        for table in ("xp_history", "player_snapshots", "character_sources", "character_person", "people"):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    finally:
        connection.close()


def test_an_untimed_event_from_an_old_addon_is_still_accepted(build_server):
    """Pre-v2.11 addons send no event_time. They can't be dated, so they are
    let through; the launch purge is the backstop for their old data."""
    _module, client = build_server(GO_LIVE_AT=GO_LIVE)
    accepted(client, "Oldie,XP,12,100,1000")
    assert leaderboard(client)["Oldie"]["level"] == 12


def test_without_a_cutoff_old_events_are_accepted(build_server):
    _module, client = build_server(GO_LIVE_AT=None)
    accepted(client, f"Beta,XP,{CUTOFF - 86400},20,100,1000")


def test_an_ignored_event_shows_in_the_traffic_log(build_server):
    _module, client = build_server(GO_LIVE_AT=GO_LIVE)
    ignored(client, f"Beta,XP,{CUTOFF - 1},20,100,1000")
    entry = client.get("/api/traffic", auth=ROSTER_AUTH).json()["entries"][0]
    assert entry["accepted"] is False
    assert "go-live" in entry["detail"]


def send(client, data, watcher_id):
    return client.post("/api/log-update", json={
        "timestamp": "2026-11-10T12:00:00", "data": data, "watcher_id": watcher_id}).json()


def test_a_re_sent_beta_queue_is_listed_once_per_install(build_server):
    """A reinstalled watcher can re-send thousands of beta events at once, which
    would push every real entry off the operator's traffic page. One entry per
    install still says who needs /ldb forget."""
    _module, client = build_server(GO_LIVE_AT=GO_LIVE)
    for i in range(5):
        assert send(client, f"Beta,XP,{CUTOFF - 100 + i},20,100,1000", "aaaa1111")["status"] == "ignored"
    send(client, f"Other,XP,{CUTOFF - 50},20,100,1000", "bbbb2222")

    entries = client.get("/api/traffic", auth=ROSTER_AUTH).json()["entries"]
    assert [e["watcher_id"] for e in entries] == ["aaaa1111", "bbbb2222"]


def test_the_watcher_moves_past_an_ignored_event_quietly(monkeypatch):
    """The watcher only treats status "error" as a refusal (and logs it); an
    "ignored" answer is delivered like a success, so a re-sent beta queue
    doesn't flood the player's watcher log. The same answer as "error" must
    raise, or this test would pass on a watcher that ignores the body."""
    import savedvars_watcher

    def answering(status):
        class Answer:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return {"status": status, "detail": "Before go-live"}
        monkeypatch.setattr(savedvars_watcher.SESSION, "post", lambda *_a, **_k: Answer())

    answering("ignored")
    savedvars_watcher.send_packet("http://example.invalid/api/log-update", "Beta,XP,1,2,3,4")

    answering("error")
    with pytest.raises(savedvars_watcher.ServerRejected):
        savedvars_watcher.send_packet("http://example.invalid/api/log-update", "Beta,XP,1,2,3,4")


def test_a_cutoff_without_a_utc_offset_refuses_to_start(build_server):
    """A bare time is read as UTC on the server: five hours off, silently."""
    with pytest.raises(RuntimeError, match="UTC offset"):
        build_server(GO_LIVE_AT="2026-11-10T18:00:00")


# --- activity follows the event's own time ------------------------------------

def test_an_old_event_does_not_make_a_character_look_active(build_server):
    """What happened at launch: a re-pushed queue reset last_activity."""
    _module, client = build_server()
    two_days_ago = int(time.time()) - 2 * 86400
    accepted(client, f"Ozzman,XP,{two_days_ago},20,100,1000")
    assert leaderboard(client)["Ozzman"]["idle_seconds"] == pytest.approx(2 * 86400, abs=60)


def test_a_fresh_event_counts_as_activity_now(build_server):
    _module, client = build_server()
    accepted(client, f"Ayla,XP,{int(time.time())},20,100,1000")
    assert leaderboard(client)["Ayla"]["idle_seconds"] == pytest.approx(0, abs=5)


def test_an_event_from_the_future_counts_as_now(build_server):
    """A player's clock running fast must not pin a character on the board."""
    module, client = build_server()
    accepted(client, f"Fast,XP,{int(time.time()) + 3 * 86400},20,100,1000")
    stamp = datetime.fromisoformat(module.player_states["Fast"]["last_activity"])
    assert stamp <= datetime.now(timezone.utc)


def test_an_untimed_event_counts_as_activity_now(build_server):
    _module, client = build_server()
    accepted(client, "Oldie,XP,12,100,1000")
    assert leaderboard(client)["Oldie"]["idle_seconds"] == pytest.approx(0, abs=5)
