# ⚔️ WoW Classic LAN Progression Dashboard

A live leaderboard for a WoW Classic LAN party. Every player's level, XP, quests, deaths, gold and played time show up on a shared dashboard as they play, with an analytics page for the numbers behind the race.

WoW addons can't make network calls or write files, so the data takes a detour:

```
LanDashboard addon ──► SavedVariables file ──► watcher (on each PC) ──► server ──► browser dashboards
  queues events          flushed on /reload       reads & forwards        stores, broadcasts over WebSocket
```

## What's in the repo

| Path | What it is |
|---|---|
| `LanDashboard/` | The in-game addon. Queues events and triggers a `/reload` at safe moments so they reach disk. Has an in-game settings panel and `/ldb` commands. |
| `savedvars_watcher.py` | Runs on each player's PC. Reads the addon's SavedVariables file and forwards new events to the server, with a local CSV backup and retry queue. Ships as a system-tray `.exe`. |
| `log_watcher.py` | Optional fallback that tails `WoWChatLog.txt`. That file only flushes when the game fully exits, so it is a catch-all, not a live feed. |
| `main.py` | FastAPI server: ingestion, SQLite storage, WebSocket broadcast, and every page below. |
| `*.html`, `static/` | The pages. No build step and no framework; d3's scale modules are vendored under `static/vendor/d3/`. |
| `tests/` | pytest suite. Runs in-process against a throwaway database per test. |

**Pages:** `/` the dashboard (ladder and big-screen views) · `/analytics` charts and per-character stats · `/roster` operator tools: roster edits, viewer access links, live ingest traffic · `/setup` player downloads and instructions · `/login`.

## Run it locally

```bash
python -m venv .venv
.venv\Scripts\activate            # source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
copy .env.example .env            # then set a real ROSTER_PASSWORD
python main.py
```

Open `http://<server-ip>:5000/`. The status badge turns green (**Live Connected**) once the page's WebSocket is up.

To send real game data to a local server rather than the hosted one, run the watcher from source in console mode. It keeps its own state next to the script, so it can run alongside the tray `.exe`:

```bash
python savedvars_watcher.py --savedvars-path "C:\...\WTF\Account\<ACCOUNT>\SavedVariables\LanDashboard.lua" --server-url "http://127.0.0.1:5000/api/log-update"
```

Use an IP address, not `localhost`. On Windows, resolving `localhost` can add seconds to every request.

## Configuration

Set in `.env` locally, or in the host's environment settings when deployed. `.env.example` has the details.

| Variable | Default | Purpose |
|---|---|---|
| `ROSTER_USERNAME` / `ROSTER_PASSWORD` | `admin` / *none* | Operator login. With no password set, operator pages refuse everyone rather than going public. |
| `DB_FILE` | `lan_progression.db` | SQLite file. Use a different file per event (beta, launch) so their data never mixes. |
| `INGESTION_TOKEN` | *unset (open)* | Shared secret every watcher must send. **Required once the server is reachable from the internet.** |
| `DOWNLOAD_PASSPHRASE` | *unset* | Gates the watcher download on `/setup`. Friends enter it and get a zip with the `.exe` and a ready-made config, so they never see the token. |
| `PUBLIC_URL` | *from request* | Address written into that bundled config, e.g. `https://4l.example.net`. |
| `STALE_AFTER_MINUTES` | `60` | How long without events before a character drops off the dashboard. |
| `SESSION_DAYS` | `30` | How long an operator or viewer sign-in lasts. |
| `SESSION_SECRET` | *derived from password* | Signs session cookies. Changing `ROSTER_PASSWORD` already signs everyone out; set this only to rotate independently. |
| `TRAFFIC_LOG_SIZE` | `300` | Ingest attempts kept in memory for the Traffic tab. |

## Players

Players only need the `/setup` page. It walks them through installing the addon and downloading the watcher. Remote friends without Python get the tray `.exe`, which finds the SavedVariables file on its own.

### Building the watcher `.exe`

Rebuild whenever `savedvars_watcher.py` changes. The server serves whatever file is in `downloads/` and can't build it itself: PyInstaller only targets the OS it runs on, and the server runs Linux.

```bash
pip install -r requirements-watcher.txt
pyinstaller --onefile --noconsole --name savedvars_watcher --distpath downloads --workpath build --specpath build savedvars_watcher.py
```

`downloads/` is git-ignored (the `.exe` is ~14 MB). `/setup` shows the file's build date and SHA-256, so a stale upload is easy to spot.

## Tests and lint

```bash
pip install -r requirements-dev.txt
python -m pytest
python -m ruff check .
```

The suite stubs out `load_dotenv`, so results never depend on your own `.env`, and it's safe to run while a server is up. `ruff.toml` explains the two rule families that are switched off.

## Deploying (Coolify)

The repo builds with the included `Dockerfile`. In Coolify: **Build Pack: Dockerfile**, **Ports Exposes: `5000`**, domain as `https://<domain>`, health check off (the slim image has no curl).

1. **Persistent volume at `/app/data`** and `DB_FILE=/app/data/lan_progression.db`. Without it, every redeploy silently wipes all progression data.
2. **Environment variables in Coolify's UI**, never a committed `.env`: at minimum `ROSTER_PASSWORD`, `DB_FILE` and `INGESTION_TOKEN`; usually `DOWNLOAD_PASSPHRASE` and `PUBLIC_URL` too.
3. **Second mount at `/app/downloads`** holding `savedvars_watcher.exe` (exactly that name), uploaded over SFTP/SCP. Replacing that file updates the download with no redeploy. Until it's there, `/setup` greys out the watcher button.

To rotate: change `DOWNLOAD_PASSPHRASE` to stop new downloads, or `INGESTION_TOKEN` to cut off existing watchers (friends then re-download).

Testing the image locally from Git Bash: prefix `docker` commands with `MSYS_NO_PATHCONV=1`, or Git Bash rewrites `/app/...` paths into Windows paths before Docker sees them.
