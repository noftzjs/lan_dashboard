"""The operator traffic monitor.

Several of this project's worst bugs were invisible from outside: a watcher
connected and forwarding nothing, probe output parsed as queued events, STATUS
payloads refused by a server that had not been redeployed. Each looked exactly
like silence. This records every ingest attempt, accepted or refused, so they
look like something instead.

Refusals matter more than acceptances: a rejected event is gone for good,
because the watcher's sent_count has already moved past it, so this is the only
place it is ever visible as it happens.
"""
import threading

import pytest
from conftest import accepted, rejected

OPERATOR = ("admin", "testpass")
STAMP = 1790000000


def receive_within(socket, seconds=5):
    """receive_json with a deadline.

    Without one, a regression that stops the broadcast does not fail the test,
    it hangs it -- receive_json waits forever for a message that will never
    come, and the whole suite never finishes. That happened while this file
    was being written: a mutation removing the refusal broadcast stalled the
    run for ten minutes and left the source file mutated behind it. A hang
    reports nothing; a failure says exactly what broke.
    """
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
        pytest.fail(f"no traffic message within {seconds}s -- the broadcast did not happen")
    return box["message"]


def traffic(client):
    response = client.get("/api/traffic", auth=OPERATOR)
    assert response.status_code == 200, response.text
    return response.json()


def test_an_accepted_event_is_recorded(build_server):
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")

    data = traffic(client)
    assert data["accepted"] == 1 and data["refused"] == 0
    entry = data["entries"][-1]
    assert entry["accepted"] is True
    assert entry["player"] == "Ayla"
    assert entry["log_type"] == "ZONE"
    assert entry["detail"] is None


def test_a_refused_event_is_recorded_with_the_reason(build_server):
    """The reason is the point. "Something was refused" sends you to the logs;
    "Level 99 outside valid range" does not."""
    _, client = build_server()
    rejected(client, f"Ayla,STATUS,{STAMP},99,0,0,0,0,0")

    data = traffic(client)
    assert data["refused"] == 1
    entry = data["entries"][-1]
    assert entry["accepted"] is False
    assert "99" in entry["detail"]
    assert entry["player"] == "Ayla"


def test_an_unparseable_payload_is_still_recorded(build_server):
    """The ones that are hardest to explain are exactly the ones worth seeing.
    A payload too broken to name a player must not be dropped silently."""
    _, client = build_server()
    rejected(client, "total nonsense")

    data = traffic(client)
    assert data["refused"] == 1
    entry = data["entries"][-1]
    assert entry["detail"]
    # Garbage must not be presented as a character name -- the monitor exists
    # to make things clearer, and a row claiming a player called "total
    # nonsense" sent a watcher does the opposite.
    assert entry["player"] is None
    assert entry["log_type"] is None
    assert entry["payload"] == "total nonsense", "the raw text is still shown, just not as a name"


def test_both_outcomes_appear_in_order(build_server):
    _, client = build_server()
    accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
    rejected(client, f"Bo,STATUS,{STAMP},99,0,0,0,0,0")
    accepted(client, f"Cy,ZONE,{STAMP},Orgrimmar")

    entries = traffic(client)["entries"]
    assert [e["player"] for e in entries] == ["Ayla", "Bo", "Cy"]
    assert [e["accepted"] for e in entries] == [True, False, True]
    assert [e["id"] for e in entries] == sorted(e["id"] for e in entries)


def test_a_long_payload_is_truncated_and_says_so(build_server):
    """An operator is scanning for shape, not reading every profession."""
    _, client = build_server()
    profs = ",".join(f"Prof{i}:1:75" for i in range(9))
    accepted(client, f"Ayla,STATUS,{STAMP},19,3445,19400,1234567,86400,3600,12.5,0,{profs}")

    entry = traffic(client)["entries"][-1]
    assert entry["truncated"] is True
    assert len(entry["payload"]) <= 160


def test_the_log_is_bounded(build_server):
    """In memory and live, so it cannot be allowed to grow forever."""
    _, client = build_server(TRAFFIC_LOG_SIZE="5")
    for i in range(12):
        accepted(client, f"P{i},ZONE,{STAMP},Ironforge")

    data = traffic(client)
    assert data["kept"] == 5
    assert data["capacity"] == 5
    # The newest survive, not the oldest.
    assert [e["player"] for e in data["entries"]] == ["P7", "P8", "P9", "P10", "P11"]


def test_it_reports_how_many_dashboards_are_watching(build_server):
    _, client = build_server()
    assert traffic(client)["connected_dashboards"] == 0


# --- who can see it ---------------------------------------------------------

def test_only_an_operator_can_read_the_traffic(build_server):
    """Payload text is not public: a refused payload can carry the gold of
    someone who chose to keep it private."""
    _, client = build_server()
    assert client.get("/api/traffic").status_code == 401


def test_a_viewer_cannot_read_the_traffic(build_server):
    _, client = build_server()
    minted = client.post("/api/access-tokens", json={"label": "v@example.com"}, auth=OPERATOR).json()
    client.get(f"/access?k={minted['token']}", follow_redirects=False)

    assert client.get("/api/traffic").status_code == 401


def test_the_socket_refuses_anyone_without_an_operator_session(build_server):
    _, client = build_server()
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/traffic"):
            pass


def test_the_socket_accepts_an_operator_session(build_server):
    _, client = build_server()
    client.post("/api/login", json={"username": "admin", "password": "testpass"})

    with client.websocket_connect("/ws/traffic") as socket:
        assert socket is not None


def test_an_accepted_event_reaches_a_listening_operator(build_server):
    """The live half: the page should not have to poll to stay current."""
    _, client = build_server()
    client.post("/api/login", json={"username": "admin", "password": "testpass"})

    with client.websocket_connect("/ws/traffic") as socket:
        accepted(client, f"Ayla,ZONE,{STAMP},Ironforge")
        message = receive_within(socket)

    assert message["type"] == "traffic"
    assert message["entry"]["player"] == "Ayla"
    assert message["entry"]["accepted"] is True


def test_a_refusal_reaches_a_listening_operator(build_server):
    _, client = build_server()
    client.post("/api/login", json={"username": "admin", "password": "testpass"})

    with client.websocket_connect("/ws/traffic") as socket:
        rejected(client, f"Ayla,STATUS,{STAMP},99,0,0,0,0,0")
        message = receive_within(socket)

    assert message["entry"]["accepted"] is False
    assert "99" in message["entry"]["detail"]
