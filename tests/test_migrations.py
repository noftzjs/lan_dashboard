"""Startup migrations.

These run against databases that already hold a LAN's worth of real history,
so getting one wrong is the most expensive kind of mistake here: the failure
is silent and the data is gone. Each test builds a database in an older shape
and checks the upgrade both adds what's missing and leaves the rows alone.
"""
import sqlite3

# A mid-era database: it has guild and current_zone, but predates
# last_activity, event_time and the stream_urls list.
LEGACY_SCHEMA = """
CREATE TABLE xp_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT, player TEXT, log_type TEXT,
    level INTEGER, current_xp INTEGER, max_xp INTEGER,
    quest_id INTEGER, xp_reward INTEGER
);
CREATE TABLE player_snapshots (
    player TEXT PRIMARY KEY,
    level INTEGER, current_xp INTEGER, max_xp INTEGER, pct REAL,
    last_updated TEXT, current_zone TEXT, faction TEXT, guild TEXT, class TEXT
);
CREATE TABLE player_roster_meta (
    player TEXT PRIMARY KEY, stream_url TEXT, tags TEXT, updated_at TEXT
);
"""


def make_legacy_db(path, rows=True):
    connection = sqlite3.connect(path)
    connection.executescript(LEGACY_SCHEMA)
    if rows:
        connection.executemany(
            "INSERT INTO player_snapshots (player, level, current_xp, max_xp, pct,"
            " last_updated, current_zone, faction, guild, class) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                # a guildless character stored with the old sentinel string
                ("Sentinel", 12, 100, 1000, 10.0, "2026-09-01T00:00:00", "Westfall",
                 "Alliance", "No Guild", "PRIEST"),
                # a zone written while the addon stamped events but the server
                # didn't yet strip the stamp
                ("Stamped", 18, 200, 2000, 10.0, "2026-09-01T00:00:00",
                 "1790125116,Ironforge", "Alliance", "Real Guild", "MAGE"),
                # a character whose guild is genuinely called "No Guild"
                ("Literal", 5, 0, 500, 0.0, "2026-09-01T00:00:00", "Durotar",
                 "Horde", "No Guild", "ROGUE"),
            ])
        connection.executemany(
            "INSERT INTO xp_history (timestamp, player, log_type, level, current_xp, max_xp)"
            " VALUES (?,?,?,?,?,?)",
            [("2026-09-01T00:00:00", "Sentinel", "XP", 12, 100, 1000)] * 5)
        connection.execute(
            "INSERT INTO player_roster_meta (player, stream_url, tags, updated_at)"
            " VALUES (?,?,?,?)",
            ("Sentinel", "https://twitch.tv/sentinel", '["Team Alpha"]', "2026-09-01T00:00:00"))
    connection.commit()
    connection.close()


def columns(path, table):
    connection = sqlite3.connect(path)
    try:
        return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def fetch(path, sql, params=()):
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in connection.execute(sql, params)]
    finally:
        connection.close()


def test_missing_columns_are_added(build_server, tmp_path):
    db = tmp_path / "legacy.db"
    make_legacy_db(db)
    assert "event_time" not in columns(db, "xp_history")

    build_server(DB_FILE=str(db))

    assert "event_time" in columns(db, "xp_history")
    assert "last_activity" in columns(db, "player_snapshots")
    assert "stream_urls" in columns(db, "player_roster_meta")


def test_existing_rows_are_preserved(build_server, tmp_path):
    db = tmp_path / "legacy.db"
    make_legacy_db(db)

    build_server(DB_FILE=str(db))

    assert len(fetch(db, "SELECT * FROM xp_history")) == 5
    assert len(fetch(db, "SELECT * FROM player_snapshots")) == 3
    # rows that predate event_time are left null rather than back-filled with
    # a guess — the analytics page relies on being able to tell them apart
    assert all(row["event_time"] is None for row in fetch(db, "SELECT * FROM xp_history"))


def test_the_no_guild_sentinel_becomes_absent(build_server, tmp_path):
    db = tmp_path / "legacy.db"
    make_legacy_db(db)

    build_server(DB_FILE=str(db))

    guilds = {row["player"]: row["guild"]
              for row in fetch(db, "SELECT player, guild FROM player_snapshots")}
    assert guilds["Sentinel"] is None
    # ...and so does a guild that really is named that, which is the known
    # cost of the old sentinel: the two were never distinguishable once stored
    assert guilds["Literal"] is None


def test_a_zone_with_a_leaked_timestamp_is_repaired(build_server, tmp_path):
    db = tmp_path / "legacy.db"
    make_legacy_db(db)

    build_server(DB_FILE=str(db))

    zones = {row["player"]: row["current_zone"]
             for row in fetch(db, "SELECT player, current_zone FROM player_snapshots")}
    assert zones["Stamped"] == "Ironforge"
    assert zones["Sentinel"] == "Westfall"       # an ordinary zone is untouched


def test_a_single_stream_link_becomes_a_list(build_server, tmp_path):
    db = tmp_path / "legacy.db"
    make_legacy_db(db)

    module, _client = build_server(DB_FILE=str(db))

    row = fetch(db, "SELECT stream_urls FROM player_roster_meta WHERE player = 'Sentinel'")[0]
    assert row["stream_urls"] == '["https://twitch.tv/sentinel"]'
    # and the in-memory state the pages read agrees
    assert module.player_states["Sentinel"]["stream_urls"] == ["https://twitch.tv/sentinel"]
    assert module.player_states["Sentinel"]["tags"] == ["Team Alpha"]


def test_migrating_twice_is_harmless(build_server, tmp_path):
    """Every restart re-runs these, so they have to be idempotent."""
    db = tmp_path / "legacy.db"
    make_legacy_db(db)

    build_server(DB_FILE=str(db))
    before = fetch(db, "SELECT * FROM player_snapshots ORDER BY player")
    build_server(DB_FILE=str(db))
    after = fetch(db, "SELECT * FROM player_snapshots ORDER BY player")

    assert before == after


def test_an_empty_database_is_created_from_scratch(build_server, tmp_path):
    db = tmp_path / "brand-new.db"
    _module, client = build_server(DB_FILE=str(db))

    assert db.exists()
    assert {"event_time", "quest_id"} <= columns(db, "xp_history")
    assert {"last_activity", "class"} <= columns(db, "player_snapshots")
    assert {"stream_urls", "tags"} <= columns(db, "player_roster_meta")
    assert client.get("/api/leaderboard").json() == []
