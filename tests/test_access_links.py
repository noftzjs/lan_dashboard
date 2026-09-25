"""Per-person access links, and the operator/viewer split.

People asked to see /analytics, and the only answer was a shared password that
also unlocks /roster -- so handing it out gave away the ability to edit. These
links let one person see the analytics page, revocably, without a login.

The cases that matter most here are the ones that must NOT work: a viewer
must not reach /roster, a tampered cookie must not become an operator, and a
revoked link must stop working.
"""
import sqlite3

OPERATOR = ("admin", "testpass")


def mint(client, label="friend@example.com", role="viewer"):
    response = client.post("/api/access-tokens", json={"label": label, "role": role}, auth=OPERATOR)
    assert response.status_code == 200, response.text
    return response.json()


def redeem(client, token):
    """Follow the link the way a browser does, keeping the cookie."""
    return client.get(f"/access?k={token}", follow_redirects=False)


# --- the happy path ------------------------------------------------------------

def test_a_link_gets_a_viewer_into_analytics(build_server):
    _, client = build_server()
    token = mint(client)["token"]

    assert client.get("/analytics").status_code == 401, "no link, no entry"
    assert redeem(client, token).status_code == 303
    assert client.get("/analytics").status_code == 200
    assert client.get("/api/analytics").status_code == 200


def test_the_token_does_not_stay_in_the_url(build_server):
    """It is exchanged for a cookie and the browser is sent somewhere clean, so
    the link cannot live on in history or a screenshot."""
    _, client = build_server()
    response = redeem(client, mint(client)["token"])

    assert response.status_code == 303
    assert response.headers["location"] == "/analytics"
    assert "k=" not in response.headers["location"]
    assert "lan_session" in response.cookies


def test_the_link_is_only_shown_once(build_server):
    """Only the hash is stored, so a leaked database hands over nothing."""
    module, client = build_server()
    token = mint(client)["token"]

    connection = sqlite3.connect(module.DB_FILE)
    try:
        stored = [r[0] for r in connection.execute("SELECT token_hash FROM access_tokens")]
    finally:
        connection.close()
    assert token not in stored
    assert all(token not in value for value in stored)

    listed = client.get("/api/access-tokens", auth=OPERATOR).json()["tokens"]
    assert all(token not in str(entry) for entry in listed)


# --- what a viewer must not be able to do --------------------------------------

def test_a_viewer_cannot_reach_the_roster(build_server):
    """The whole reason for this feature: sharing the analytics page must not
    hand over the ability to edit."""
    _, client = build_server()
    redeem(client, mint(client)["token"])

    assert client.get("/roster").status_code == 401
    assert client.post("/api/roster/Ayla", json={"tags": ["x"]}).status_code == 401


def test_a_tampered_cookie_is_not_an_operator(build_server):
    """Sent as an explicit header rather than through the cookie jar: setting a
    cookie on a client that already holds a valid one sends both, and the
    server reads the good one -- which made an earlier version of this test
    pass without proving anything."""
    _, client = build_server()
    redeem(client, mint(client)["token"])
    good = client.cookies["lan_session"]
    client.cookies.clear()

    tampered = {"Cookie": f"lan_session={good.replace('viewer', 'operator', 1)}"}
    assert client.get("/roster", headers=tampered).status_code == 401, "editing the role must not promote"
    assert client.get("/analytics", headers=tampered).status_code == 401, "a broken signature is not a viewer either"

    # The untouched cookie still works, so the refusal above is the tampering.
    assert client.get("/analytics", headers={"Cookie": f"lan_session={good}"}).status_code == 200


def test_garbage_cookies_are_refused(build_server):
    _, client = build_server()
    for value in ("", "nonsense", "operator:9999999999:deadbeef", "operator:notanumber:x",
                  "viewer:9999999999:", ":::"):
        assert client.get("/analytics",
                          headers={"Cookie": f"lan_session={value}"}).status_code == 401,             f"accepted {value!r}"


def test_an_expired_session_stops_working(build_server):
    """The expired cookie is minted directly and sent as a header.

    Going through the browser flow with a negative SESSION_DAYS proves nothing:
    the cookie's max-age goes negative, the client throws it away, and the 401
    comes from having no cookie at all rather than from the server checking the
    expiry. An earlier version of this test did exactly that, and passed with
    the expiry check removed entirely."""
    module, client = build_server()

    original = module.SESSION_DAYS
    try:
        module.SESSION_DAYS = -1                     # minted already stale
        expired = module.make_session(module.ROLE_VIEWER)
        module.SESSION_DAYS = original
        fresh = module.make_session(module.ROLE_VIEWER)
    finally:
        module.SESSION_DAYS = original

    assert client.get("/analytics",
                      headers={"Cookie": f"lan_session={expired}"}).status_code == 401
    # The same cookie with a live expiry works, so the refusal is the expiry
    # and not something else about how it was made.
    assert client.get("/analytics",
                      headers={"Cookie": f"lan_session={fresh}"}).status_code == 200


# --- revocation ------------------------------------------------------------------

def test_a_revoked_link_stops_working(build_server):
    _, client = build_server()
    minted = mint(client)
    token_id = client.get("/api/access-tokens", auth=OPERATOR).json()["tokens"][0]["id"]

    assert client.post(f"/api/access-tokens/{token_id}/revoke", auth=OPERATOR).status_code == 200
    assert redeem(client, minted["token"]).status_code == 403


def test_revoking_one_link_leaves_the_others_alone(build_server):
    """Revoking per person is the point; a shared password could not do this."""
    _, client = build_server()
    keep = mint(client, "keep@example.com")["token"]
    drop = mint(client, "drop@example.com")["token"]

    listed = client.get("/api/access-tokens", auth=OPERATOR).json()["tokens"]
    drop_id = next(t["id"] for t in listed if t["label"] == "drop@example.com")
    client.post(f"/api/access-tokens/{drop_id}/revoke", auth=OPERATOR)

    assert redeem(client, drop).status_code == 403
    assert redeem(client, keep).status_code == 303


def test_an_unknown_link_and_a_revoked_one_answer_alike(build_server):
    """Different answers would let someone probe for live tokens."""
    _, client = build_server()
    minted = mint(client)
    token_id = client.get("/api/access-tokens", auth=OPERATOR).json()["tokens"][0]["id"]
    client.post(f"/api/access-tokens/{token_id}/revoke", auth=OPERATOR)

    revoked = redeem(client, minted["token"])
    unknown = redeem(client, "a" * 40)
    assert revoked.status_code == unknown.status_code == 403
    assert revoked.json()["detail"] == unknown.json()["detail"]


# --- the operator is not locked out ---------------------------------------------

def test_the_password_still_works_everywhere(build_server):
    """Additive: nothing an operator could reach before is closed to them now."""
    _, client = build_server()
    assert client.get("/roster", auth=OPERATOR).status_code == 200
    assert client.get("/analytics", auth=OPERATOR).status_code == 200
    assert client.get("/api/analytics", auth=OPERATOR).status_code == 200


def test_only_an_operator_can_mint_or_revoke(build_server):
    _, client = build_server()
    redeem(client, mint(client)["token"])          # now holding a viewer cookie

    assert client.post("/api/access-tokens", json={"label": "x"}).status_code == 401
    assert client.get("/api/access-tokens").status_code == 401
    assert client.post("/api/access-tokens/abc123/revoke").status_code == 401


def test_signing_out_drops_the_session(build_server):
    _, client = build_server()
    redeem(client, mint(client)["token"])
    assert client.get("/analytics").status_code == 200

    client.post("/api/sign-out")
    assert client.get("/analytics").status_code == 401


# --- input handling ---------------------------------------------------------------

def test_a_label_is_required_and_bounded(build_server):
    _, client = build_server()
    assert client.post("/api/access-tokens", json={"label": "   "}, auth=OPERATOR).status_code == 400
    assert client.post("/api/access-tokens", json={"label": "x" * 500}, auth=OPERATOR).status_code == 400


def test_an_unknown_role_is_refused(build_server):
    """So a typo cannot quietly mint something with no role, or an admin one."""
    _, client = build_server()
    assert client.post("/api/access-tokens", json={"label": "a@b.c", "role": "admin"},
                       auth=OPERATOR).status_code == 400


def test_the_public_dashboard_is_untouched(build_server):
    _, client = build_server()
    assert client.get("/").status_code == 200
    assert client.get("/api/leaderboard").status_code == 200
