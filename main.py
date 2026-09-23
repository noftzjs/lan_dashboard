import contextlib
import hashlib
import io
import json
import logging
import os
import secrets
import time
import zipfile
from datetime import datetime, timezone
from typing import Any

import aiosqlite
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lan_dashboard")

load_dotenv()

app = FastAPI(title="WoW LAN Progression Dashboard")

# --- ROSTER MANAGER AUTH ---
# Only /roster and its editing endpoint need this — the read-only
# leaderboard/websocket are meant for every spectator.
roster_auth = HTTPBasic()
ROSTER_USERNAME = os.environ.get("ROSTER_USERNAME", "admin")
ROSTER_PASSWORD = os.environ.get("ROSTER_PASSWORD")


def safe_equals(a: str, b: str) -> bool:
    """Constant-time string comparison that can't blow up on odd input.
    secrets.compare_digest raises TypeError for non-ASCII *str* arguments, so
    a header or password containing one stray character would turn a plain
    "wrong credentials" into a 500 — compare the UTF-8 bytes instead."""
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def require_roster_auth(credentials: HTTPBasicCredentials = Depends(roster_auth)):
    if not ROSTER_PASSWORD:
        # Fail closed, not open: an unset password must never mean "no auth
        # required," or the roster manager silently becomes public.
        raise HTTPException(
            status_code=500,
            detail="ROSTER_PASSWORD is not set. Create a .env file (see .env.example) before running the server.",
        )
    # secrets.compare_digest avoids leaking match-length via response timing.
    valid_username = safe_equals(credentials.username, ROSTER_USERNAME)
    valid_password = safe_equals(credentials.password, ROSTER_PASSWORD)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# --- INGESTION TOKEN (optional) ---
# /api/log-update has to stay reachable without interactive login, since
# every player's watcher script posts to it unattended. On a trusted LAN
# that's fine wide open — but once this server is reachable from the
# internet (see Coolify deployment), anyone who finds the URL could inject
# fake data. Unlike ROSTER_PASSWORD, this deliberately does NOT fail closed
# when unset: a LAN-only deployment shouldn't have to configure a token it
# doesn't need, so leaving INGESTION_TOKEN unset keeps today's open
# behavior exactly as it was. Set it (same value on the server and every
# watcher) before exposing this beyond a trusted local network.
INGESTION_TOKEN = os.environ.get("INGESTION_TOKEN")


def require_ingestion_token(x_ingestion_token: str | None = Header(None)):
    if not INGESTION_TOKEN:
        return  # not configured — LAN-open mode, unchanged from before
    if not safe_equals(x_ingestion_token or "", INGESTION_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid X-Ingestion-Token header")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Lets mock/beta/live data live in separate files (e.g. DB_FILE=beta.db)
# instead of one file accumulating everything — set per-deployment in .env.
DB_FILE = os.environ.get("DB_FILE", "lan_progression.db")

# How long a character can go without any accepted event (ZONE/PROFILE/XP/
# QUEST) before the dashboard treats it as stale and hides it — e.g. a bank
# alt someone logged into and left parked. Note the clock here is when the
# server *hears* about activity, and delivery is reload-gated (the addon's
# sync), so a player who's actively playing but hasn't synced recently also
# looks stale; raise this if that hides people who are really playing.
STALE_AFTER_SECONDS = float(os.environ.get("STALE_AFTER_MINUTES", "60")) * 60
player_states: dict[str, dict[str, Any]] = {}

def idle_seconds(state: dict[str, Any]) -> float | None:
    """Seconds since this character's last accepted event, or None if the
    server has never seen one (treated as stale by the client)."""
    last = state.get("last_activity")
    if not last:
        return None
    return max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds())

def public_state(state: dict[str, Any]) -> dict[str, Any]:
    # The client can't compare last_activity to its own clock (server and
    # browser can be in different timezones or drift), so ship an age the
    # client only has to add its own elapsed-since-receipt time to.
    return {**state, "idle_seconds": idle_seconds(state)}

# --- DATABASE LIFECYCLE ---
@app.on_event("startup")
async def startup_event():
    """Initializes the SQLite database tables on server boot."""
    async with aiosqlite.connect(DB_FILE) as db:
        # Table to store every granular historical event for graphing later
        await db.execute("""
            CREATE TABLE IF NOT EXISTS xp_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                player TEXT,
                log_type TEXT,
                level INTEGER,
                current_xp INTEGER,
                max_xp INTEGER,
                quest_id INTEGER,
                xp_reward INTEGER,
                event_time TEXT
            )
        """)
        # event_time is when the event happened in game (addon v2.11.0+); the
        # older `timestamp` column is when the watcher delivered the batch, which
        # collapses a whole session onto a few sync instants. Rows written before
        # this existed keep event_time NULL — chart over it with that in mind.
        cursor = await db.execute("PRAGMA table_info(xp_history)")
        if "event_time" not in {row[1] for row in await cursor.fetchall()}:
            await db.execute("ALTER TABLE xp_history ADD COLUMN event_time TEXT")
        # Table to preserve state if the server restarts mid-LAN
        await db.execute("""
            CREATE TABLE IF NOT EXISTS player_snapshots (
                player TEXT PRIMARY KEY,
                level INTEGER,
                current_xp INTEGER,
                max_xp INTEGER,
                pct REAL,
                last_updated TEXT,
                current_zone TEXT,
                faction TEXT,
                guild TEXT,
                class TEXT,
                last_activity TEXT
            )
        """)
        # Migrate older databases created before these columns were tracked.
        cursor = await db.execute("PRAGMA table_info(player_snapshots)")
        existing_columns = {row[1] for row in await cursor.fetchall()}
        for column in ("current_zone", "faction", "guild", "class", "last_activity"):
            if column not in existing_columns:
                await db.execute(f"ALTER TABLE player_snapshots ADD COLUMN {column} TEXT")

        # One-time cleanup: older addon/mock-generator versions stored the
        # literal string "No Guild" for guildless characters, indistinguishable
        # from a real guild actually named that. NULL is the correct "absent"
        # value now (see the PROFILE handler below) — fix any rows already
        # written before this was corrected.
        await db.execute("UPDATE player_snapshots SET guild = NULL WHERE guild = 'No Guild'")

        # Operator-curated metadata the game has no way of telling us:
        # a streaming link and freeform tags (group assignment, "LAN local", etc).
        await db.execute("""
            CREATE TABLE IF NOT EXISTS player_roster_meta (
                player TEXT PRIMARY KEY,
                stream_url TEXT,
                stream_urls TEXT,
                tags TEXT,
                updated_at TEXT
            )
        """)
        # stream_url held exactly one link; stream_urls is a JSON list so a
        # player can have, say, a Twitch and a Kick. The old column is kept
        # and migrated from rather than dropped, so a rollback still reads.
        cursor = await db.execute("PRAGMA table_info(player_roster_meta)")
        if "stream_urls" not in {row[1] for row in await cursor.fetchall()}:
            await db.execute("ALTER TABLE player_roster_meta ADD COLUMN stream_urls TEXT")
        await db.execute("""
            UPDATE player_roster_meta
               SET stream_urls = json_array(stream_url)
             WHERE stream_urls IS NULL AND stream_url IS NOT NULL AND stream_url <> ''
        """)

        # One-time cleanup for zones stored during the window when the addon
        # already stamped events (v2.11.0+) but this server hadn't been
        # redeployed to strip that stamp — those rows kept the raw
        # "<epoch>,Zone Name" string. A zone only re-emits when a character
        # actually moves, so without this they'd stay wrong for a long time.
        await db.execute("""
            UPDATE player_snapshots
               SET current_zone = substr(current_zone, instr(current_zone, ',') + 1)
             WHERE current_zone GLOB '[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9],*'
        """)
        await db.commit()

    # Hydrate our global memory cache from the database if data already exists
    await reload_states_from_db()

async def reload_states_from_db():
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM player_snapshots") as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                player_states[row["player"]] = {
                    "level": row["level"],
                    "current_xp": row["current_xp"],
                    "max_xp": row["max_xp"],
                    "pct": row["pct"],
                    "last_updated": row["last_updated"],
                    "current_zone": row["current_zone"],
                    "faction": row["faction"],
                    "guild": row["guild"],
                    "class": row["class"],
                    "last_activity": row["last_activity"],
                    "stream_urls": [],
                    "tags": [],
                }

        async with db.execute("SELECT * FROM player_roster_meta") as cursor:
            rows = await cursor.fetchall()
            for row in rows:
                state = player_states.setdefault(row["player"], default_state())
                # Prefer the list; fall back to the single column for a row
                # written before the migration ran.
                if row["stream_urls"]:
                    state["stream_urls"] = json.loads(row["stream_urls"])
                elif row["stream_url"]:
                    state["stream_urls"] = [row["stream_url"]]
                state["tags"] = json.loads(row["tags"]) if row["tags"] else []

# --- WEBSOCKET CONNECTION MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            # A spectator whose tab just closed shouldn't stop the broadcast
            # reaching everyone else; the socket is dropped from the list by
            # disconnect() when its own endpoint coroutine notices.
            with contextlib.suppress(Exception):
                await connection.send_json(message)

manager = ConnectionManager()

class LogPayload(BaseModel):
    timestamp: str
    data: str

class RosterMetaPayload(BaseModel):
    # Fields left as None are unchanged; send "" / [] explicitly to clear one.
    stream_url: str | None = None      # legacy single link, still accepted
    stream_urls: list[str] | None = None
    tags: list[str] | None = None

# Mirrored in static/dashboard.js for immediate UI feedback, but this is the
# boundary that actually matters: the API is callable directly regardless of
# what the browser sends. A single unbroken tag with no spaces can't wrap in
# the UI, so an unbounded one stretches a card/row arbitrarily wide.
TAG_MAX_LENGTH = 30
TAGS_MAX_COUNT = 3
STREAM_URL_MAX_LENGTH = 300
# More than a few badges crowds the name line on both dashboard layouts.
STREAM_URLS_MAX_COUNT = 3

# /api/log-update is just parsing a CSV string with no game-state to check
# it against, so nothing stops a malformed or malicious payload from
# claiming to be level 69 or an invalid class. Validate everything that has
# a known-good shape; classic level cap and the 9 original classes only —
# update VALID_CLASSES if the addon/dashboard ever supports another version.
VALID_FACTIONS = {"Alliance", "Horde"}
VALID_CLASSES = {"WARRIOR", "PALADIN", "HUNTER", "ROGUE", "PRIEST", "SHAMAN", "MAGE", "WARLOCK", "DRUID"}
LEVEL_MIN, LEVEL_MAX = 1, 60
PLAYER_NAME_MAX_LENGTH = 24
GUILD_MAX_LENGTH = 100
ZONE_MAX_LENGTH = 100
XP_SANITY_CEILING = 10_000_000  # not a real curve check, just rejects garbage magnitudes

# --- INGESTION ENDPOINT ---
def default_state():
    # A function, not a module-level dict: "tags" is a list, and a shallow
    # dict(DEFAULT_STATE) copy would leave every player sharing the same
    # list object, so mutating one player's tags would leak into all of them.
    return {
        "level": 1, "current_xp": 0, "max_xp": 1, "pct": 0,
        "last_updated": None, "current_zone": None, "faction": None, "guild": None,
        "class": None, "last_activity": None, "stream_urls": [], "tags": [],
    }


# Addon v2.11.0+ stamps each event with the realm clock, inserted straight after
# the log type. Older addons don't, and at a LAN not everyone updates at once, so
# the field is sniffed rather than assumed: only a bare 10-digit integer in that
# slot is a timestamp. Nothing else can look like one — faction is a word, level
# is 1-60, a quest id is at most 5 digits, and a zone name isn't all digits — and
# the range check keeps a stray number from being read as a date.
EVENT_TIME_MIN = 1_577_836_800  # 2020-01-01, comfortably before this project existed
EVENT_TIME_MAX = 4_102_444_800  # 2100-01-01


def extract_event_time_field(rest: str) -> datetime | None:
    """The in-game time this event happened, or None for a pre-v2.11.0 addon."""
    candidate, _, _ = rest.partition(",")
    candidate = candidate.strip()
    if len(candidate) != 10 or not candidate.isdigit():
        return None
    epoch = int(candidate)
    if not (EVENT_TIME_MIN <= epoch <= EVENT_TIME_MAX):
        return None
    return datetime.fromtimestamp(epoch, timezone.utc)


@app.post("/api/log-update")
async def receive_log_update(payload: LogPayload, _auth: None = Depends(require_ingestion_token)):
    try:
        # Only split off the player name and log type here — the remainder is
        # split per-type below so free-text fields (guild/zone names) can
        # safely contain commas without shifting later fields.
        header, _, rest = payload.data.partition(",")
        player_name = header.strip()
        log_type, _, rest = rest.partition(",")
        log_type = log_type.strip()

        if not player_name or not log_type:
            raise ValueError(f"Malformed payload, missing name/type: {payload.data!r}")
        if len(player_name) > PLAYER_NAME_MAX_LENGTH:
            raise ValueError(f"Player name too long ({len(player_name)} chars): {player_name!r}")

        event_time = extract_event_time_field(rest)
        if event_time is not None:
            _, _, rest = rest.partition(",")

        async with aiosqlite.connect(DB_FILE) as db:
            if log_type == "ZONE":
                zone_name = rest.strip()
                if len(zone_name) > ZONE_MAX_LENGTH:
                    raise ValueError(f"Zone name too long ({len(zone_name)} chars): {zone_name!r}")
                state = player_states.setdefault(player_name, default_state())
                state["current_zone"] = zone_name
                await db.execute("""
                    INSERT INTO player_snapshots (player, level, current_xp, max_xp, pct, last_updated, current_zone)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(player) DO UPDATE SET current_zone=excluded.current_zone
                """, (player_name, state["level"], state["current_xp"], state["max_xp"], state["pct"],
                      state["last_updated"], zone_name))

            elif log_type == "PROFILE":
                # Guild names may legitimately contain commas, so only split
                # off faction and class, and treat the remainder as the guild name.
                faction, _, rest2 = rest.partition(",")
                class_token, _, guild = rest2.partition(",")
                faction, class_token, guild = faction.strip(), class_token.strip(), guild.strip()
                if faction not in VALID_FACTIONS:
                    raise ValueError(f"Invalid faction {faction!r}")
                if class_token not in VALID_CLASSES:
                    raise ValueError(f"Invalid class {class_token!r}")
                if len(guild) > GUILD_MAX_LENGTH:
                    raise ValueError(f"Guild name too long ({len(guild)} chars): {guild!r}")
                # Empty guild means "not in a guild" — store it as None like
                # every other optional field, not as an empty string, so the
                # existing "if player.guild" checks throughout the frontend
                # keep working regardless of which falsy value shows up.
                guild = guild or None
                state = player_states.setdefault(player_name, default_state())
                state["faction"] = faction
                state["class"] = class_token
                state["guild"] = guild
                await db.execute("""
                    INSERT INTO player_snapshots
                        (player, level, current_xp, max_xp, pct, last_updated, faction, class, guild)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(player) DO UPDATE SET
                        faction=excluded.faction, class=excluded.class, guild=excluded.guild
                """, (player_name, state["level"], state["current_xp"], state["max_xp"], state["pct"],
                      state["last_updated"], faction, class_token, guild))

            elif log_type == "XP":
                level_str, current_xp_str, max_xp_str = (p.strip() for p in rest.split(",", 2))
                level, current_xp, max_xp = int(level_str), int(current_xp_str), int(max_xp_str)
                if not (LEVEL_MIN <= level <= LEVEL_MAX):
                    raise ValueError(f"Level {level} outside valid range {LEVEL_MIN}-{LEVEL_MAX}")
                if not (0 <= current_xp <= XP_SANITY_CEILING):
                    raise ValueError(f"current_xp {current_xp} outside sane range")
                if not (1 <= max_xp <= XP_SANITY_CEILING):
                    raise ValueError(f"max_xp {max_xp} outside sane range")
                if current_xp > max_xp:
                    raise ValueError(f"current_xp {current_xp} exceeds max_xp {max_xp}")
                pct = round((current_xp / max_xp) * 100, 2) if max_xp > 0 else 0
                last_updated = datetime.now().isoformat()

                state = player_states.setdefault(player_name, default_state())
                state.update({
                    "level": level, "current_xp": current_xp, "max_xp": max_xp,
                    "pct": pct, "last_updated": last_updated,
                })

                # Update Snapshot table (Upsert)
                await db.execute("""
                    INSERT INTO player_snapshots (player, level, current_xp, max_xp, pct, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(player) DO UPDATE SET
                        level=excluded.level, current_xp=excluded.current_xp,
                        max_xp=excluded.max_xp, pct=excluded.pct,
                        last_updated=excluded.last_updated
                """, (player_name, level, current_xp, max_xp, pct, last_updated))

                # Append to granular historical timeline
                await db.execute("""
                    INSERT INTO xp_history (timestamp, player, log_type, level, current_xp, max_xp, event_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (payload.timestamp, player_name, log_type, level, current_xp, max_xp,
                      event_time.isoformat() if event_time else None))

            elif log_type == "QUEST":
                quest_id_str, xp_reward_str = (p.strip() for p in rest.split(",", 1))
                quest_id, xp_reward = int(quest_id_str), int(xp_reward_str)
                if quest_id < 0:
                    raise ValueError(f"Negative quest_id {quest_id}")
                if not (0 <= xp_reward <= XP_SANITY_CEILING):
                    raise ValueError(f"xp_reward {xp_reward} outside sane range")
                await db.execute("""
                    INSERT INTO xp_history (timestamp, player, log_type, quest_id, xp_reward, event_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (payload.timestamp, player_name, log_type, quest_id, xp_reward,
                      event_time.isoformat() if event_time else None))

            elif log_type == "DEATH":
                # level,zone — zone is the remainder, so a comma in a zone name
                # (none today, but the same rule as guild names) stays intact.
                level_str, _, death_zone = rest.partition(",")
                level = int(level_str.strip())
                death_zone = death_zone.strip()
                if not (LEVEL_MIN <= level <= LEVEL_MAX):
                    raise ValueError(f"Level {level} outside valid range {LEVEL_MIN}-{LEVEL_MAX}")
                if len(death_zone) > ZONE_MAX_LENGTH:
                    raise ValueError(f"Zone name too long ({len(death_zone)} chars): {death_zone!r}")
                player_states.setdefault(player_name, default_state())
                await db.execute("""
                    INSERT INTO xp_history (timestamp, player, log_type, level, event_time)
                    VALUES (?, ?, ?, ?, ?)
                """, (payload.timestamp, player_name, log_type, level,
                      event_time.isoformat() if event_time else None))

            else:
                raise ValueError(f"Unknown log_type {log_type!r} in payload: {payload.data!r}")

            # Reaching here means the payload passed validation, so count it
            # as activity for the stale-card check. UTC + ISO so it survives a
            # restart (persisted below) and parses back unambiguously. QUEST
            # never touched player_states/player_snapshots before, so this
            # upsert is also what creates a row for a quest-only first event.
            state = player_states.setdefault(player_name, default_state())
            state["last_activity"] = datetime.now(timezone.utc).isoformat()
            await db.execute("""
                INSERT INTO player_snapshots (player, level, current_xp, max_xp, pct, last_updated, last_activity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(player) DO UPDATE SET last_activity=excluded.last_activity
            """, (player_name, state["level"], state["current_xp"], state["max_xp"], state["pct"],
                  state["last_updated"], state["last_activity"]))

            await db.commit()

        # Broadcast the data out live to viewers
        await manager.broadcast({
            "event": "PLAYER_UPDATE",
            "player": player_name,
            "state": public_state(player_states[player_name]),
        })

        return {"status": "success"}
    except Exception as e:
        logger.exception("Failed to process log-update payload: %r", payload.data)
        return {"status": "error", "detail": str(e)}

# --- OPERATOR-CURATED ROSTER METADATA ---
# Streaming links and tags aren't something the game can tell us — they're
# set by whoever runs the dashboard, so this (unlike /api/log-update) requires
# roster credentials.
@app.post("/api/roster/{player_name}")
async def update_roster_meta(player_name: str, payload: RosterMetaPayload, _user: str = Depends(require_roster_auth)):
    state = player_states.setdefault(player_name, default_state())
    # stream_urls wins when both are sent; stream_url keeps an older client
    # (or a stale browser tab) working by being treated as a one-item list.
    incoming_streams = payload.stream_urls
    if incoming_streams is None and payload.stream_url is not None:
        incoming_streams = [payload.stream_url]
    if incoming_streams is not None:
        state["stream_urls"] = [
            url.strip()[:STREAM_URL_MAX_LENGTH] for url in incoming_streams if url.strip()
        ][:STREAM_URLS_MAX_COUNT]
    if payload.tags is not None:
        state["tags"] = [t.strip()[:TAG_MAX_LENGTH] for t in payload.tags if t.strip()][:TAGS_MAX_COUNT]

    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            INSERT INTO player_roster_meta (player, stream_url, stream_urls, tags, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(player) DO UPDATE SET
                stream_url=excluded.stream_url, stream_urls=excluded.stream_urls,
                tags=excluded.tags, updated_at=excluded.updated_at
        """, (player_name,
              state["stream_urls"][0] if state["stream_urls"] else None,   # keeps the legacy column usable
              json.dumps(state["stream_urls"]), json.dumps(state["tags"]),
              datetime.now().isoformat()))
        await db.commit()

    await manager.broadcast({
        "event": "PLAYER_UPDATE",
        "player": player_name,
        "state": public_state(state),
    })
    return {"status": "success", "state": public_state(state)}


PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(PROJECT_DIR, "static")

# Mount only the dedicated assets folder, never the project root — the
# latter also contains lan_progression.db, source files, and watcher logs,
# none of which should be reachable over HTTP.
app.mount("/static", StaticFiles(directory=ASSETS_DIR), name="static")


@app.get("/")
async def get_dashboard():
    """Serves the dashboard so spectators just browse to the server's
    address instead of needing index.html copied onto their machine."""
    return FileResponse(os.path.join(PROJECT_DIR, "index.html"))


@app.get("/roster")
async def get_roster_page(_user: str = Depends(require_roster_auth)):
    """Operator-facing roster manager for bulk-editing stream links/tags."""
    return FileResponse(os.path.join(PROJECT_DIR, "roster.html"))


# --- SETUP PAGE & DOWNLOADS ---
# Public on purpose: players who aren't on the LAN need these before they can
# do anything else. Nothing secret is served — the ingestion token is never
# included; players get it from the operator and paste it into the setup page,
# which only uses it in their own browser to write their config file.
#
# The addon zip is built from LanDashboard/ on every request, so the download
# can't drift from the source folder. The watcher .exe can't be built here
# (PyInstaller only produces binaries for the OS it runs on, and this server
# may be a Linux container), so it's a prebuilt file in downloads/ — rebuild
# and replace it whenever savedvars_watcher.py changes (see README).
ADDON_DIR = os.path.join(PROJECT_DIR, "LanDashboard")
DOWNLOADS_DIR = os.path.join(PROJECT_DIR, "downloads")
WATCHER_EXE = os.path.join(DOWNLOADS_DIR, "savedvars_watcher.exe")
ADDON_FILE_EXTENSIONS = (".lua", ".toc", ".xml")
_watcher_hash_cache: dict[str, Any] = {}

# Optional passphrase that gates the watcher download. When set, the bare .exe
# is no longer served; instead POST /api/watcher-bundle returns a zip holding
# the .exe plus a ready-made config (server address and, if one is set, the
# ingestion token) — so friends who were given the passphrase never see or
# handle the token at all. The token is only ever emitted through this gate:
# with no passphrase configured the bundle endpoint doesn't exist, so an
# unset passphrase can never leak it.
DOWNLOAD_PASSPHRASE = os.environ.get("DOWNLOAD_PASSPHRASE")
# The address friends should use, e.g. https://4l.noftz.net. Optional: without
# it the address is derived from the request, which works behind a reverse
# proxy as long as it forwards Host and X-Forwarded-Proto (Coolify's does).
PUBLIC_URL = os.environ.get("PUBLIC_URL")

# The passphrase is guessable by anyone who can reach the site, so cap wrong
# guesses. A global window (not per-IP) because behind a proxy every client
# can share one address; the cost is that someone hammering the endpoint can
# briefly block friends too, which beats an unbounded guessing rate.
PASSPHRASE_MAX_FAILURES = 10
PASSPHRASE_WINDOW_SECONDS = 60
_passphrase_failures: list[float] = []


def passphrase_locked_out() -> bool:
    cutoff = time.monotonic() - PASSPHRASE_WINDOW_SECONDS
    _passphrase_failures[:] = [t for t in _passphrase_failures if t > cutoff]
    return len(_passphrase_failures) >= PASSPHRASE_MAX_FAILURES


def public_base_url(request: Request) -> str:
    if PUBLIC_URL:
        return PUBLIC_URL.rstrip("/")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}"


def addon_version() -> str | None:
    try:
        with open(os.path.join(ADDON_DIR, "LanDashboard.toc"), encoding="utf-8") as f:
            for line in f:
                if line.startswith("## Version:"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def watcher_info() -> dict[str, Any] | None:
    """Size, SHA-256 and build time of the hosted .exe, or None if it isn't
    there. The hash is what lets a player (or Windows SmartScreen-wary
    friend) verify the download; cached by mtime/size so it isn't recomputed
    on every page view."""
    try:
        stat = os.stat(WATCHER_EXE)
    except OSError:
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    if _watcher_hash_cache.get("key") != key:
        digest = hashlib.sha256()
        with open(WATCHER_EXE, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
        _watcher_hash_cache.update(key=key, sha256=digest.hexdigest())
    return {
        "size": stat.st_size,
        "sha256": _watcher_hash_cache["sha256"],
        "built": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


@app.get("/setup")
async def get_setup_page():
    """Player-facing setup guide: install the addon, run the watcher."""
    return FileResponse(os.path.join(PROJECT_DIR, "setup.html"))


@app.get("/api/setup-info")
def get_setup_info():
    return {
        "ingestion_token_required": bool(INGESTION_TOKEN),
        "passphrase_required": bool(DOWNLOAD_PASSPHRASE),
        "stale_after_minutes": STALE_AFTER_SECONDS / 60,
        "addon": {"version": addon_version()},
        "watcher": watcher_info(),
    }


@app.get("/download/LanDashboard.zip")
def download_addon():
    if not os.path.isdir(ADDON_DIR):
        raise HTTPException(status_code=404, detail="Addon files are not available on this server.")
    names = sorted(n for n in os.listdir(ADDON_DIR) if n.lower().endswith(ADDON_FILE_EXTENSIONS))
    if not names:
        raise HTTPException(status_code=404, detail="Addon files are not available on this server.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            # Nested under LanDashboard/ so extracting into Interface\AddOns
            # yields exactly the folder WoW expects.
            archive.write(os.path.join(ADDON_DIR, name), arcname=f"LanDashboard/{name}")
    version = addon_version() or "latest"
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="LanDashboard-v{version}.zip"'},
    )


@app.get("/download/savedvars_watcher.exe")
def download_watcher():
    if DOWNLOAD_PASSPHRASE:
        raise HTTPException(status_code=403, detail="This download needs the passphrase. Use the Setup page.")
    if not os.path.isfile(WATCHER_EXE):
        raise HTTPException(status_code=404, detail="The watcher download is not available on this server yet.")
    return FileResponse(WATCHER_EXE, media_type="application/octet-stream", filename="savedvars_watcher.exe")


BUNDLE_README = """LAN Dashboard Watcher
=====================

1. In WoW, log in and click "Sync LAN Dashboard" (or type /reload) once, so the
   addon saves its data file. The watcher can't find anything before that.
2. Double-click savedvars_watcher.exe. No window opens -- it runs in your system
   tray (bottom-right of the taskbar; click the ^ arrow if you don't see it).
3. Right-click the tray icon for status, to open the dashboard or the log, or to quit.

Keep savedvars_watcher_config.json in the same folder as the .exe. It holds this
dashboard's address and access token, so don't post it anywhere public.
"""


class BundleRequest(BaseModel):
    passphrase: str


@app.post("/api/watcher-bundle")
def download_watcher_bundle(payload: BundleRequest, request: Request):
    if not DOWNLOAD_PASSPHRASE:
        raise HTTPException(status_code=404, detail="Not available.")
    if passphrase_locked_out():
        raise HTTPException(status_code=429, detail="Too many incorrect attempts. Wait a minute and try again.")
    if not safe_equals(payload.passphrase.strip(), DOWNLOAD_PASSPHRASE):
        _passphrase_failures.append(time.monotonic())
        raise HTTPException(status_code=401, detail="That passphrase isn't right.")
    if not os.path.isfile(WATCHER_EXE):
        raise HTTPException(status_code=404, detail="The watcher download is not available on this server yet.")

    config = {"server_url": f"{public_base_url(request)}/api/log-update"}
    if INGESTION_TOKEN:
        config["ingestion_token"] = INGESTION_TOKEN

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        # The .exe is already compressed inside; storing it avoids burning CPU
        # for no size win on every download.
        archive.write(WATCHER_EXE, arcname="savedvars_watcher.exe", compress_type=zipfile.ZIP_STORED)
        archive.writestr("savedvars_watcher_config.json", json.dumps(config, indent=2) + "\n")
        archive.writestr("README.txt", BUNDLE_README)
    return Response(
        content=buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="LanDashboardWatcher.zip"'},
    )


@app.get("/analytics")
async def get_analytics_page():
    """Progression analytics over the stored history."""
    return FileResponse(os.path.join(PROJECT_DIR, "analytics.html"))


# Quest reward bands. Boundaries are round numbers a player recognises rather
# than computed quantiles, so the buckets mean the same thing run to run.
QUEST_BANDS = [(0, 250), (250, 500), (500, 1000), (1000, 2000), (2000, XP_SANITY_CEILING)]


def summarise_xp_rows(rows):
    """Total XP observed per player, walking each character's XP events in
    order. Two cases carry XP: progress within a level, and a level-up, where
    the character finished the old level (max_xp - last seen) and then earned
    whatever it has in the new one. Only ever adds forward progress, so a
    reset or a re-read can't produce a negative total."""
    gained: dict[str, int] = {}
    previous: dict[str, dict] = {}
    for row in rows:
        player = row["player"]
        last = previous.get(player)
        if last is not None:
            if row["level"] == last["level"] and row["current_xp"] > last["current_xp"]:
                gained[player] = gained.get(player, 0) + (row["current_xp"] - last["current_xp"])
            elif row["level"] > last["level"]:
                finished = max(0, (last["max_xp"] or 0) - last["current_xp"])
                gained[player] = gained.get(player, 0) + finished + row["current_xp"]
        previous[player] = {"level": row["level"], "current_xp": row["current_xp"], "max_xp": row["max_xp"]}
    return gained


@app.get("/api/analytics")
async def get_analytics():
    """Aggregates for the analytics page. Computed per request by reading the
    history table — fine at LAN scale (tens of thousands of rows at most); if
    this ever gets slow, cache it per DB write rather than sampling."""
    async with aiosqlite.connect(DB_FILE) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT player, level, current_xp, max_xp FROM xp_history WHERE log_type='XP' ORDER BY id"
        ) as cursor:
            xp_rows = await cursor.fetchall()
        async with db.execute(
            "SELECT player, xp_reward FROM xp_history WHERE log_type='QUEST' AND xp_reward IS NOT NULL"
        ) as cursor:
            quest_rows = await cursor.fetchall()
        # The XP each level costs is a game constant, so any player who has
        # been through a level tells us its price.
        async with db.execute(
            "SELECT level, MAX(max_xp) AS cost FROM xp_history"
            " WHERE log_type='XP' AND max_xp > 1 GROUP BY level ORDER BY level"
        ) as cursor:
            level_rows = await cursor.fetchall()
        async with db.execute(
            "SELECT player, level, event_time FROM xp_history"
            " WHERE log_type='XP' AND event_time IS NOT NULL ORDER BY event_time"
        ) as cursor:
            timed_rows = await cursor.fetchall()
        async with db.execute("SELECT COUNT(*) AS n FROM xp_history") as cursor:
            history_total = (await cursor.fetchone())["n"]
        async with db.execute(
            "SELECT player, COUNT(*) AS deaths FROM xp_history WHERE log_type='DEATH' GROUP BY player"
        ) as cursor:
            death_rows = await cursor.fetchall()
    deaths = {row["player"]: row["deaths"] for row in death_rows}

    gained = summarise_xp_rows(xp_rows)

    # Levels gained *while this dashboard was watching* — a character first
    # seen at 10 and now 17 gained 7 here, not 16. Counting level-1 would
    # credit us with levels earned before anyone installed the addon.
    seen_levels: dict[str, list] = {}
    for row in xp_rows:
        low, high = seen_levels.get(row["player"], (row["level"], row["level"]))
        seen_levels[row["player"]] = (min(low, row["level"]), max(high, row["level"]))
    levels_gained = sum(high - low for low, high in seen_levels.values())

    quest_count: dict[str, int] = {}
    quest_xp: dict[str, int] = {}
    for row in quest_rows:
        quest_count[row["player"]] = quest_count.get(row["player"], 0) + 1
        quest_xp[row["player"]] = quest_xp.get(row["player"], 0) + row["xp_reward"]

    players = []
    for name, state in player_states.items():
        total = gained.get(name, 0)
        quests_xp = quest_xp.get(name, 0)
        players.append({
            "name": name,
            "class": state.get("class"),
            "faction": state.get("faction"),
            "guild": state.get("guild"),
            "level": state.get("level", 1),
            "pct": state.get("pct", 0),
            "zone": state.get("current_zone"),
            "idle_seconds": idle_seconds(state),
            "quests": quest_count.get(name, 0),
            "deaths": deaths.get(name, 0),
            "quest_xp": quests_xp,
            # Quest XP is reported by the game, while the total is inferred from
            # XP snapshots, so a character whose quest turn-ins arrived but
            # whose XP ticks didn't could otherwise show negative "other".
            "other_xp": max(0, total - quests_xp),
            "total_xp": total,
        })
    players.sort(key=lambda p: (p["level"], p["pct"]), reverse=True)

    all_rewards = [row["xp_reward"] for row in quest_rows]
    banded_total = sum(all_rewards)
    quest_bands = []
    for low, high in QUEST_BANDS:
        in_band = [value for value in all_rewards if low <= value < high]
        quest_bands.append({
            "low": low,
            "high": high,
            "count": len(in_band),
            "xp": sum(in_band),
            "share": round(100 * sum(in_band) / banded_total, 1) if banded_total else 0,
        })

    # One line per character, but only for players with at least two separate
    # moments recorded — a single point is not a trend.
    velocity: dict[str, list] = {}
    for row in timed_rows:
        velocity.setdefault(row["player"], []).append({"at": row["event_time"], "level": row["level"]})
    velocity = {
        name: points for name, points in velocity.items()
        if len({point["at"] for point in points}) >= 2
    }

    return {
        "totals": {
            "characters": len(players),
            "levels_gained": levels_gained,
            "quests": len(all_rewards),
            "quest_xp": banded_total,
            "xp_recorded": sum(gained.values()),
            "deaths": sum(deaths.values()),
        },
        "players": players,
        "quest_bands": quest_bands,
        "level_costs": [{"level": row["level"], "xp": row["cost"]} for row in level_rows],
        "velocity": velocity,
        "history": {"rows": history_total, "timed_rows": len(timed_rows)},
        "stale_after_seconds": STALE_AFTER_SECONDS,
    }


@app.get("/api/leaderboard")
async def get_leaderboard(sort_by: str = Query("level"), search: str = Query(None)):
    players = [
        {"name": name, **public_state(stats)}
        for name, stats in player_states.items()
        if not search or search.lower() in name.lower()
    ]
    players.sort(
        key=lambda x: (x["level"], x["pct"]) if sort_by == "level" else x["name"].lower(),
        reverse=(sort_by == "level"),
    )
    return players

@app.websocket("/ws/dashboard")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        await websocket.send_json({
            "event": "INIT",
            "data": {name: public_state(state) for name, state in player_states.items()},
            "stale_after_seconds": STALE_AFTER_SECONDS,
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5000)
