"""Who can reach what. Three independent mechanisms guard three different
things, and each has a deliberate failure direction:

  ROSTER_PASSWORD     fails CLOSED  (unset -> 500, never "no auth needed")
  INGESTION_TOKEN     fails OPEN    (unset -> endpoint stays public, so a
                                     LAN deployment needs no configuration)
  DOWNLOAD_PASSPHRASE gates the watcher bundle, and when it is unset the
                      bundle endpoint does not exist at all — so an unset
                      passphrase can never leak the ingestion token.
"""
import io
import json
import zipfile

import pytest
from conftest import ROSTER_AUTH, post_event

PASSPHRASE = "correct horse battery"


# --- the ingestion endpoint --------------------------------------------------

def test_ingestion_is_open_when_no_token_is_configured(build_server):
    _module, client = build_server(INGESTION_TOKEN=None)
    assert post_event(client, "Open,ZONE,Elwynn Forest").json()["status"] == "success"


def test_ingestion_requires_the_token_once_one_is_set(build_server):
    _module, client = build_server(INGESTION_TOKEN="s3cret")
    assert post_event(client, "Gated,ZONE,Elwynn Forest").status_code == 401
    assert post_event(client, "Gated,ZONE,Elwynn Forest", token="wrong").status_code == 401
    assert post_event(client, "Gated,ZONE,Elwynn Forest", token="s3cret").json()["status"] == "success"


def test_a_non_ascii_token_is_refused_not_a_server_error(build_server):
    """secrets.compare_digest raises TypeError on non-ASCII str, which turned
    a wrong token into a 500 until the comparison moved to bytes.

    Sent as raw bytes because that's the only way it can travel: HTTP header
    values are latin-1 on the wire, so a well-behaved client won't encode a
    str containing these, but the server still has to survive receiving them.
    """
    _module, client = build_server(INGESTION_TOKEN="s3cret")
    response = client.post(
        "/api/log-update",
        json={"timestamp": "t", "data": "X,ZONE,Y"},
        headers={"X-Ingestion-Token": "tökén".encode("latin-1")},
    )
    assert response.status_code == 401


# --- the roster manager ------------------------------------------------------

@pytest.mark.parametrize("path", ["/roster", "/analytics", "/api/analytics"])
def test_operator_pages_require_credentials(server, path):
    assert server.get(path).status_code == 401
    assert server.get(path, auth=ROSTER_AUTH).status_code == 200


@pytest.mark.parametrize("path", ["/", "/setup", "/api/setup-info", "/api/leaderboard",
                                  "/download/LanDashboard.zip"])
def test_spectator_facing_routes_stay_public(server, path):
    assert server.get(path).status_code == 200


def test_roster_editing_requires_credentials(server):
    assert server.post("/api/roster/Someone", json={"tags": ["x"]}).status_code == 401
    assert server.post("/api/roster/Someone", json={"tags": ["x"]}, auth=ROSTER_AUTH).status_code == 200


def test_an_unset_roster_password_fails_closed(build_server):
    """Never "no password set, so let everyone in"."""
    _module, client = build_server(ROSTER_PASSWORD=None)
    assert client.get("/roster", auth=("admin", "anything")).status_code == 500


def test_a_non_ascii_password_is_refused_not_a_server_error(server):
    assert server.get("/roster", auth=("admin", "pässword")).status_code == 401


# --- the passphrase-gated watcher bundle -------------------------------------

def test_without_a_passphrase_the_bundle_endpoint_does_not_exist(build_server):
    """The bundle is the only thing that ever emits the ingestion token, so
    an unconfigured passphrase must not simply mean "no check"."""
    _module, client = build_server(DOWNLOAD_PASSPHRASE=None, INGESTION_TOKEN="s3cret")
    for attempt in ("", "anything", "s3cret"):
        assert client.post("/api/watcher-bundle", json={"passphrase": attempt}).status_code == 404


def test_the_bare_exe_is_locked_once_a_passphrase_is_set(build_server):
    _module, client = build_server(DOWNLOAD_PASSPHRASE=PASSPHRASE)
    assert client.get("/download/savedvars_watcher.exe").status_code == 403


def test_a_wrong_passphrase_is_refused(build_server):
    _module, client = build_server(DOWNLOAD_PASSPHRASE=PASSPHRASE)
    assert client.post("/api/watcher-bundle", json={"passphrase": "nope"}).status_code == 401
    # non-ASCII must behave the same way rather than erroring
    assert client.post("/api/watcher-bundle", json={"passphrase": "nöpe"}).status_code == 401


def test_repeated_wrong_guesses_are_rate_limited(build_server):
    _module, client = build_server(DOWNLOAD_PASSPHRASE=PASSPHRASE)
    codes = [client.post("/api/watcher-bundle", json={"passphrase": f"guess{i}"}).status_code
             for i in range(12)]
    assert codes[0] == 401
    assert 429 in codes
    # the lockout holds even for the right passphrase, by design
    assert client.post("/api/watcher-bundle", json={"passphrase": PASSPHRASE}).status_code == 429


def test_the_bundle_carries_a_ready_made_config(build_server, monkeypatch, tmp_path):
    """A friend should be able to unzip and run with nothing to edit — which
    is what lets the token stay out of their hands entirely."""
    module, client = build_server(DOWNLOAD_PASSPHRASE=PASSPHRASE,
                                  INGESTION_TOKEN="s3cret",
                                  PUBLIC_URL="https://4l.example.net")
    fake_exe = tmp_path / "savedvars_watcher.exe"
    fake_exe.write_bytes(b"MZ fake binary")
    monkeypatch.setattr(module, "WATCHER_EXE", str(fake_exe))

    response = client.post("/api/watcher-bundle", json={"passphrase": f"  {PASSPHRASE}  "})
    assert response.status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert sorted(archive.namelist()) == ["README.txt", "savedvars_watcher.exe",
                                          "savedvars_watcher_config.json"]
    config = json.loads(archive.read("savedvars_watcher_config.json"))
    assert config["server_url"] == "https://4l.example.net/api/log-update"
    assert config["ingestion_token"] == "s3cret"
    assert archive.read("savedvars_watcher.exe") == b"MZ fake binary"


def test_the_bundle_url_follows_the_proxy_headers_when_public_url_is_unset(build_server, monkeypatch, tmp_path):
    """Behind Coolify's proxy the request arrives as plain http; the address
    friends actually use comes from the forwarded headers."""
    module, client = build_server(DOWNLOAD_PASSPHRASE=PASSPHRASE, PUBLIC_URL=None)
    fake_exe = tmp_path / "savedvars_watcher.exe"
    fake_exe.write_bytes(b"MZ")
    monkeypatch.setattr(module, "WATCHER_EXE", str(fake_exe))

    response = client.post("/api/watcher-bundle", json={"passphrase": PASSPHRASE},
                           headers={"Host": "4l.example.net", "X-Forwarded-Proto": "https"})
    config = json.loads(zipfile.ZipFile(io.BytesIO(response.content))
                        .read("savedvars_watcher_config.json"))
    assert config["server_url"] == "https://4l.example.net/api/log-update"


def test_a_missing_exe_degrades_instead_of_breaking_the_page(build_server, monkeypatch):
    """`downloads/` is deliberately not in git, so a fresh deploy has no exe
    until one is uploaded. The site must still work."""
    module, client = build_server(DOWNLOAD_PASSPHRASE=PASSPHRASE)
    monkeypatch.setattr(module, "WATCHER_EXE", "does-not-exist.exe")
    assert client.get("/setup").status_code == 200
    assert client.get("/api/setup-info").json()["watcher"] is None
    assert client.post("/api/watcher-bundle", json={"passphrase": PASSPHRASE}).status_code == 404


def test_setup_info_advertises_what_the_page_needs_to_know(build_server):
    _module, client = build_server(DOWNLOAD_PASSPHRASE=PASSPHRASE, INGESTION_TOKEN="s3cret")
    info = client.get("/api/setup-info").json()
    assert info["passphrase_required"] is True
    assert info["ingestion_token_required"] is True
    assert info["addon"]["version"]            # read from the .toc
