"""The watcher's half of linking characters to people: the ids it sends.

End to end with the real server, too: the watcher's own send_packet posts
through a TestClient, so a mismatch in field names between the two sides
fails here rather than silently linking nothing in production.
"""
import re

from conftest import ROSTER_AUTH

import savedvars_watcher as watcher

REAL_PATH = (r"C:\Program Files (x86)\World of Warcraft\_classic_beta_"
             r"\WTF\Account\2238600#10\SavedVariables\LanDashboard.lua")
STAMP = 1790000000


# --- account id -----------------------------------------------------------------

def test_the_account_id_is_a_short_hex_hash():
    account = watcher.account_id_for(REAL_PATH)
    assert re.fullmatch(r"[0-9a-f]{16}", account)


def test_the_real_account_name_is_not_sent():
    account = watcher.account_id_for(REAL_PATH)
    assert "2238600" not in account


def test_every_character_on_an_account_gets_the_same_id():
    """It is the folder, not the file, that identifies the account -- and a
    second install of the game on another drive is still the same account."""
    other_install = r"D:\Games\World of Warcraft\_classic_beta_\WTF\Account\2238600#10\SavedVariables\LanDashboard.lua"
    assert watcher.account_id_for(REAL_PATH) == watcher.account_id_for(other_install)


def test_different_accounts_get_different_ids():
    other = REAL_PATH.replace("2238600#10", "2238600#11")
    assert watcher.account_id_for(REAL_PATH) != watcher.account_id_for(other)


def test_case_and_slashes_do_not_change_the_id():
    """Windows ignores case in paths, and the tray's file picker returns
    forward slashes where discovery returns backslashes.

    A lettered account name on purpose: older accounts use one (MYACCOUNT),
    and the numeric 2238600#10 has no case to change, so a test using it
    passed even with case sensitivity broken."""
    named = r"C:\WoW\_classic_\WTF\Account\MYACCOUNT\SavedVariables\LanDashboard.lua"
    variant = named.lower().replace("\\", "/")
    assert watcher.account_id_for(named) == watcher.account_id_for(variant)


def test_a_path_without_an_account_folder_gives_no_id():
    """Better no link than a wrong one."""
    assert watcher.account_id_for(r"C:\Users\me\Downloads\LanDashboard.lua") is None
    assert watcher.account_id_for(None) is None
    assert watcher.account_id_for("") is None


# --- install id -----------------------------------------------------------------

def test_the_install_id_is_made_once_and_kept(tmp_path):
    path = str(tmp_path / ".savedvars_watcher_id")
    first = watcher.load_watcher_id(path)
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert watcher.load_watcher_id(path) == first, "a restart must not look like a new PC"


def test_two_installs_get_different_ids(tmp_path):
    a = watcher.load_watcher_id(str(tmp_path / "a"))
    b = watcher.load_watcher_id(str(tmp_path / "b"))
    assert a != b


def test_a_corrupted_id_file_is_replaced(tmp_path):
    path = tmp_path / ".savedvars_watcher_id"
    path.write_text("not an id", encoding="utf-8")
    new_id = watcher.load_watcher_id(str(path))
    assert re.fullmatch(r"[0-9a-f]{32}", new_id)
    assert path.read_text(encoding="utf-8") == new_id


def test_an_unwritable_folder_still_gives_an_id(tmp_path, monkeypatch):
    """Data must still flow; only the link is lost on the next restart."""
    monkeypatch.setattr(watcher, "log_error", lambda _message: None)
    missing_dir = str(tmp_path / "does-not-exist" / ".savedvars_watcher_id")
    assert re.fullmatch(r"[0-9a-f]{32}", watcher.load_watcher_id(missing_dir))


# --- sending ----------------------------------------------------------------------

class ThroughTestClient:
    """Stands in for requests.Session so send_packet posts to the real app."""

    def __init__(self, client):
        self.client = client

    def post(self, _url, json, headers, **_kwargs):
        return self.client.post("/api/log-update", json=json, headers=headers)


def test_the_server_links_what_the_watcher_sends(build_server, tmp_path, monkeypatch):
    _, client = build_server()
    monkeypatch.setattr(watcher, "SESSION", ThroughTestClient(client))
    watcher_id = watcher.load_watcher_id(str(tmp_path / "id"))
    source = {"watcher_id": watcher_id, "account_id": watcher.account_id_for(REAL_PATH)}

    watcher.send_packet("unused", f"Cyklades Usa,ZONE,{STAMP},Ironforge", source=source)
    watcher.send_packet("unused", f"Alt Char,ZONE,{STAMP},Orgrimmar", source=source)

    (person,) = client.get("/api/people", auth=ROSTER_AUTH).json()["people"]
    assert person["characters"] == ["Alt Char", "Cyklades Usa"]
    assert person["watcher_ids"] == [watcher_id]
    assert person["account_ids"] == [source["account_id"]]


def test_sending_without_ids_still_works(build_server, monkeypatch):
    """No source at all -- e.g. no account folder in the path -- must not
    break delivery or send empty fields."""
    _, client = build_server()
    monkeypatch.setattr(watcher, "SESSION", ThroughTestClient(client))
    watcher.send_packet("unused", f"Ayla,ZONE,{STAMP},Ironforge", source={"watcher_id": None, "account_id": None})

    entry = client.get("/api/traffic", auth=ROSTER_AUTH).json()["entries"][-1]
    assert entry["accepted"] is True
    assert entry["watcher_id"] is None
