"""The operator login form.

This exists to get browsers off HTTP Basic. Basic has no log out -- once the
browser has a credential it keeps sending it and will not re-prompt -- which
made a cached credential look like a broken password during local testing. A
cookie can be dropped; a Basic credential cannot.

So pages redirect here instead of sending a WWW-Authenticate challenge, which
is what triggers the browser's own password box in the first place.
"""
import pytest

GOOD = {"username": "admin", "password": "testpass"}


def test_signing_in_sets_a_session(build_server):
    _, client = build_server()
    response = client.post("/api/login", json=GOOD)

    assert response.status_code == 200
    assert "lan_session" in response.cookies
    assert client.get("/roster", follow_redirects=False).status_code == 200


def test_the_wrong_password_is_refused(build_server):
    _, client = build_server()
    assert client.post("/api/login", json={**GOOD, "password": "nope"}).status_code == 401
    assert client.get("/roster", follow_redirects=False).status_code == 303


def test_a_wrong_username_and_a_wrong_password_look_alike(build_server):
    """Different messages would turn the form into a way of finding out which
    usernames exist."""
    _, client = build_server()
    bad_user = client.post("/api/login", json={"username": "nobody", "password": "testpass"})
    bad_pass = client.post("/api/login", json={**GOOD, "password": "nope"})

    assert bad_user.status_code == bad_pass.status_code == 401
    assert bad_user.json()["detail"] == bad_pass.json()["detail"]


def test_a_non_ascii_password_is_refused_not_a_server_error(build_server):
    """safe_equals compares bytes; the stdlib version raises on non-ASCII str."""
    _, client = build_server()
    assert client.post("/api/login", json={**GOOD, "password": "pässword"}).status_code == 401


def test_repeated_failures_are_rate_limited(build_server):
    """The form is reachable by anyone who can reach the site, so guessing has
    to cost something."""
    _, client = build_server()
    for _ in range(10):
        client.post("/api/login", json={**GOOD, "password": "nope"})

    assert client.post("/api/login", json={**GOOD, "password": "nope"}).status_code == 429
    # Locked out means locked out: the right password does not slip through.
    assert client.post("/api/login", json=GOOD).status_code == 429


def test_signing_out_ends_the_session(build_server):
    _, client = build_server()
    client.post("/api/login", json=GOOD)
    assert client.get("/roster", follow_redirects=False).status_code == 200

    client.post("/api/sign-out")
    assert client.get("/roster", follow_redirects=False).status_code == 303


def test_an_unset_password_fails_closed(build_server):
    _, client = build_server(ROSTER_PASSWORD=None)
    assert client.post("/api/login", json=GOOD).status_code == 500


# --- where it sends you afterwards ---------------------------------------------

def test_it_returns_you_to_the_page_you_asked_for(build_server):
    _, client = build_server()
    landing = client.get("/analytics", follow_redirects=False)
    assert landing.headers["location"] == "/login?next=%2Fanalytics"

    assert client.post("/api/login", json={**GOOD, "next": "/analytics"}).json()["next"] == "/analytics"


@pytest.mark.parametrize("target", [
    "https://evil.example/phish",
    "//evil.example/phish",
    "http://evil.example",
])
def test_it_cannot_be_used_to_redirect_off_site(build_server, target):
    """Otherwise /login?next=<anywhere> is an open redirect that borrows this
    site's name to send people somewhere else."""
    _, client = build_server()
    assert client.post("/api/login", json={**GOOD, "next": target}).json()["next"] == "/roster"


def test_a_relative_path_is_still_allowed(build_server):
    _, client = build_server()
    assert client.post("/api/login", json={**GOOD, "next": "/roster"}).json()["next"] == "/roster"


# --- the login page itself --------------------------------------------------------

def test_the_login_page_is_public(build_server):
    """It has to be reachable by someone with no session, or there is no way in."""
    _, client = build_server()
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


def test_whoami_reports_the_current_role(build_server):
    _, client = build_server()
    assert client.get("/api/whoami").json()["role"] is None

    client.post("/api/login", json=GOOD)
    assert client.get("/api/whoami").json()["role"] == "operator"
