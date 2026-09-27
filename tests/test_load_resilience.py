"""Behaviour that load testing showed the server needs under pressure.

Found with loadtest.py against a local server, not guessed:
  * 200 watchers at once had 8% of events refused as "database is locked" --
    and a refused event is skipped by the watcher for good. A busy database
    must be "try again", never "refused".
  * Sending to dashboards happened inside every ingest request, one socket at
    a time, so a dashboard that stopped reading could hold up every watcher.
    Fan-out is now queued per socket, and a socket that cannot keep up is
    dropped (it reconnects and reloads) instead of waited on.
"""
import asyncio
import sqlite3

import requests
from conftest import ROSTER_AUTH, accepted

import savedvars_watcher as watcher

STAMP = 1790000000


# --- a busy database is "retry", not "refused" ---------------------------------

def test_a_busy_database_asks_the_watcher_to_retry(build_server, monkeypatch):
    module, client = build_server()

    def locked(_statements):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(module, "_apply_writes", locked)
    response = client.post("/api/log-update", json={"timestamp": "t", "data": f"Ayla,ZONE,{STAMP},Ironforge"})

    assert response.status_code == 503
    assert response.json()["status"] == "retry", "must not look like a refusal to the watcher"
    entry = client.get("/api/traffic", auth=ROSTER_AUTH).json()["entries"][-1]
    assert entry["accepted"] is False
    assert "retry" in entry["detail"]


def test_the_watcher_keeps_an_event_the_server_asked_it_to_retry():
    """The other half: a 503 must reach the watcher as a connection-type
    failure (kept and resent), not as ServerRejected (skipped for good).
    Checked against the real send_packet, since that distinction is the whole
    point and lives in the watcher."""

    class Busy:
        status_code = 503

        def raise_for_status(self):
            raise requests.HTTPError("503 Service Unavailable")

        def json(self):
            return {"status": "retry"}

    class Session:
        def post(self, *_args, **_kwargs):
            return Busy()

    original = watcher.SESSION
    watcher.SESSION = Session()
    try:
        try:
            watcher.send_packet("http://unused", f"Ayla,ZONE,{STAMP},Ironforge")
        except watcher.ServerRejected:
            raise AssertionError("a busy server was treated as a refusal -- the event would be lost") from None
        except requests.exceptions.RequestException:
            pass                          # kept, and retried on the next poll
        else:
            raise AssertionError("a 503 was treated as delivered")
    finally:
        watcher.SESSION = original


def test_a_failure_part_way_through_an_event_writes_nothing(build_server, monkeypatch):
    """Statements are collected and applied only once the whole event has
    been processed. A ZONE event queues its history row, then looks the
    character up a second time to stamp its activity -- so failing that
    second lookup, standing in for any unexpected error mid-event, must leave
    no half of the event in the database."""
    module, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")

    class FailsOnSecondLookup(dict):
        lookups = 0

        def setdefault(self, key, default=None):
            FailsOnSecondLookup.lookups += 1
            if FailsOnSecondLookup.lookups == 2:
                raise RuntimeError("mid-event failure")
            return super().setdefault(key, default)

    monkeypatch.setattr(module, "player_states", FailsOnSecondLookup(module.player_states))
    result = client.post("/api/log-update", json={"timestamp": "t", "data": f"Ayla,ZONE,{STAMP},Orgrimmar"}).json()
    assert result["status"] == "error"

    with sqlite3.connect(module.DB_FILE) as db:
        zones = db.execute("SELECT zone FROM xp_history WHERE player='Ayla' AND log_type='ZONE'").fetchall()
    assert zones == [("Ironforge",)], "the failed event's history row must not have been written"


def test_the_database_runs_in_wal_mode(build_server):
    """So reads (analytics) and writes (ingest) stop blocking each other."""
    module, _client = build_server()
    with sqlite3.connect(module.DB_FILE) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


# --- fan-out never waits on a slow dashboard -------------------------------------

class FakeSocket:
    """Stands in for a Starlette WebSocket. stall=True never finishes a send."""

    def __init__(self, stall=False):
        self.stall = stall
        self.sent = []
        self.closed_with = None

    async def accept(self):
        pass

    async def send_text(self, text):
        if self.stall:
            await asyncio.Event().wait()
        self.sent.append(text)

    async def close(self, code=1000):
        self.closed_with = code


def run(coro):
    """Runs a scenario on a fresh loop, then winds down the writer tasks it
    left waiting, so none is destroyed while still pending."""
    loop = asyncio.new_event_loop()
    try:
        # Capped: a regression that makes broadcast wait on the stalled socket
        # must fail these tests, not hang the suite behind them.
        result = loop.run_until_complete(asyncio.wait_for(coro, 5.0))
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:                       # an empty gather() belongs to no loop in 3.10
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        return result
    finally:
        loop.close()


def test_broadcast_returns_without_waiting_on_a_stalled_dashboard(build_server):
    module, _client = build_server()

    async def scenario():
        manager = module.ConnectionManager()
        stalled, healthy = FakeSocket(stall=True), FakeSocket()
        await manager.connect(stalled)
        await manager.connect(healthy)
        started = asyncio.get_running_loop().time()
        for i in range(20):
            # Bounded, so a broadcast that does wait on the stalled socket
            # fails this test instead of hanging the whole suite.
            await asyncio.wait_for(manager.broadcast({"n": i}), 1.0)
        elapsed = asyncio.get_running_loop().time() - started
        await asyncio.sleep(0.05)
        return elapsed, healthy.sent

    elapsed, delivered = run(scenario())
    assert elapsed < 0.1, "an ingest request must never wait on a browser"
    assert len(delivered) == 20, "the healthy dashboard still gets everything"


def test_a_dashboard_that_falls_behind_is_dropped_not_waited_on(build_server, monkeypatch):
    module, _client = build_server()
    monkeypatch.setattr(module.ConnectionManager, "MAX_QUEUED", 3)

    async def scenario():
        manager = module.ConnectionManager()
        stalled = FakeSocket(stall=True)
        await manager.connect(stalled)
        for i in range(10):
            await manager.broadcast({"n": i})
        await asyncio.sleep(0.05)
        return manager.active_connections, stalled.closed_with

    remaining, close_code = run(scenario())
    assert remaining == [], "it is let go, and its page reconnects with a fresh snapshot"
    assert close_code == 1013, "closed as 'try again later', so the page knows to reconnect"


def test_a_dashboard_too_slow_to_take_one_message_is_dropped(build_server, monkeypatch):
    """A laptop that went to sleep: nothing piles up, the send just never ends."""
    module, _client = build_server()
    monkeypatch.setattr(module.ConnectionManager, "SEND_TIMEOUT", 0.1)

    async def scenario():
        manager = module.ConnectionManager()
        await manager.connect(FakeSocket(stall=True))
        await manager.broadcast({"n": 1})
        await asyncio.sleep(0.4)
        return manager.active_connections

    assert run(scenario()) == []


def test_the_first_message_always_arrives_first(build_server):
    """A new dashboard's snapshot must come before any update, or an older
    snapshot could overwrite a newer update on screen."""
    module, _client = build_server()

    async def scenario():
        manager = module.ConnectionManager()
        socket = FakeSocket()
        await manager.connect(socket, first_message={"event": "INIT"})
        await manager.broadcast({"event": "PLAYER_UPDATE", "n": 1})
        await manager.broadcast({"event": "PLAYER_UPDATE", "n": 2})
        await asyncio.sleep(0.05)
        return socket.sent

    sent = run(scenario())
    assert sent == ['{"event": "INIT"}',
                    '{"event": "PLAYER_UPDATE", "n": 1}',
                    '{"event": "PLAYER_UPDATE", "n": 2}']
