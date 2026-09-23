"""
Reads the LanDashboard addon's SavedVariables file and forwards new queued
events to the dashboard server.

This exists because log_watcher.py's approach (tailing WoWChatLog.txt) turned
out to have a hard platform limitation: that file only flushes to disk on a
full client exit, never on /reload — so it can't deliver anything close to
live data during an actual play session (confirmed empirically, not assumed).
SavedVariables DOES flush on /reload, which is exactly what the addon's
reload-trigger system (see LanDashboard.lua) is built around: it queues
every PROFILE/ZONE/XP/QUEST event into LanDashboardDB.events and calls
ReloadUI() at scored "safe windows" (flight paths, AFK, loading screens),
each of which flushes the whole queue to this file.

Unlike a log file, a SavedVariables file is fully REWRITTEN on every save,
not appended to — so this watcher re-reads and re-parses the whole (small)
file on every poll, rather than tailing it, and tracks how many of the
events in it have already been forwarded.

Usage:
    python savedvars_watcher.py
        --savedvars-path "C:\\...\\WTF\\Account\\YOURACCOUNT\\SavedVariables\\LanDashboard.lua"
        --server-url "http://<server-ip>:5000/api/log-update"

Settings can also be stored in savedvars_watcher_config.json next to this
script so players don't need to pass flags every LAN:
    {
      "savedvars_path": "C:\\...\\SavedVariables\\LanDashboard.lua",
      "server_url": "http://127.0.0.1:5000/api/log-update",
      "poll_interval": 5
    }
CLI flags override the config file. If --savedvars-path isn't passed and
there's no config file, this scans common WoW install locations across every
drive letter and either uses the one match it finds, offers a numbered
picker for multiple matches, or falls back to an interactive prompt —
this is what makes a packaged .exe usable without asking a friend to find
and paste a file path themselves.

Two ways to run:
  * Console (the default when run as a script): prints its status and may
    prompt, as above.
  * System tray (--tray, and always for the packaged .exe): no window at all,
    just a tray icon whose colour and menu show status. There's no console to
    prompt on, so it never asks anything — it picks the most recently used
    account if it finds several, and keeps looking until the addon's first
    save creates the file. Needs the extra packages in requirements-watcher.txt.
"""
import argparse
import contextlib
import glob
import json
import os
import re
import string
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime

import requests

# Windows terminals default to the legacy cp1252 code page, which can't
# encode arbitrary unicode player/guild names that might get echoed into
# error messages below. Force UTF-8 output where supported (Python 3.7+).
# line_buffering=True too: this script runs an infinite polling loop with
# no natural exit, so anything less than line-buffered output (the default
# outside an interactive terminal) could sit unflushed indefinitely —
# including the warnings below that matter most when something's wrong.
# (A windowed .exe has no stdout at all, hence the None check.)
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

# A PyInstaller-frozen .exe extracts to a temp dir and __file__ resolves
# there, not next to the .exe — anything written by SCRIPT_DIR path would
# vanish when the process exits. sys.executable is the actual .exe path
# in that case, so config/state/backup files land next to it instead,
# where a player can actually find them between runs.
SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "savedvars_watcher_config.json")
STATE_PATH = os.path.join(SCRIPT_DIR, ".savedvars_watcher_state.json")
BACKUP_CSV = os.path.join(SCRIPT_DIR, "savedvars_watcher_backup.csv")
ERROR_LOG = os.path.join(SCRIPT_DIR, "savedvars_watcher_errors.log")
# Everything the watcher says, not just errors — the only place to read it
# in tray mode, where there's no console.
LOG_PATH = os.path.join(SCRIPT_DIR, "savedvars_watcher.log")
LOG_MAX_BYTES = 512 * 1024
SINGLE_INSTANCE_MUTEX = "Local\\LanDashboardWatcher"
# How often tray mode re-scans the drives while it hasn't found the addon's
# save file yet. Not every poll: the scan touches every drive letter.
DISCOVERY_SCAN_SECONDS = 30

# Matches each quoted Lua string literal in order. WoW serializes a plain
# sequential array (our events table) as bare quoted strings one per line —
# not `["1"] = "...",` keyed entries — so position in the file IS the index.
STRING_LITERAL_RE = re.compile(r'"((?:\\.|[^"\\])*)"')

# Set to a TrayUI while running in tray mode; the helpers below then mirror
# status and notable events to the tray icon. None in console mode.
TRAY = None


def _append(path, line):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass  # a log line must never be the thing that kills the watcher


def say(message):
    """Console (when there is one) plus the log file."""
    if sys.stdout is not None:
        print(f"[savedvars-watcher] {message}")
    _append(LOG_PATH, f"{datetime.now().isoformat(timespec='seconds')}\t{message}\n")


def log_error(message):
    _append(ERROR_LOG, f"{datetime.now().isoformat()}\t{message}\n")
    say(message)


def set_status(text, level="ok"):
    """level is 'ok', 'wait' or 'error' — the tray icon's colour."""
    if TRAY:
        TRAY.set_status(text, level)


def notify(message):
    if TRAY:
        TRAY.notify(message)


def discover_savedvars_paths():
    """Scan common WoW install locations across every drive letter for the
    addon's SavedVariables file. Exists so a packaged .exe doesn't require a
    friend who's never seen this codebase to hunt down and hand-type an
    install path — most players have no reason to know that folder exists.
    Returns a list of matches (possibly empty); order is not meaningful."""
    found = []
    program_files_names = ["Program Files (x86)", "Program Files"]
    for drive_letter in string.ascii_uppercase:
        drive = f"{drive_letter}:\\"
        if not os.path.isdir(drive):
            continue
        roots = [os.path.join(drive, pf, "World of Warcraft") for pf in program_files_names]
        # Some players install straight to a drive root to dodge Program
        # Files permission quirks, or under a Games folder — check those too,
        # not just the default.
        roots.append(os.path.join(drive, "World of Warcraft"))
        roots.append(os.path.join(drive, "Games", "World of Warcraft"))
        for root in roots:
            if not os.path.isdir(root):
                continue
            pattern = os.path.join(root, "_*_", "WTF", "Account", "*", "SavedVariables", "LanDashboard.lua")
            found.extend(glob.glob(pattern))
    # De-dupe (e.g. a symlinked drive) while preserving scan order.
    seen = set()
    unique = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def most_recently_used(paths):
    """The path whose file changed last — with several accounts or installs,
    that's the one the player is actually using."""
    def mtime(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0
    return max(paths, key=mtime)


class ConfigError(Exception):
    pass


def parse_args():
    parser = argparse.ArgumentParser(description="Forward LanDashboard SavedVariables events to the dashboard server.")
    parser.add_argument("--savedvars-path",
                        help="Path to the addon's SavedVariables file (.../SavedVariables/LanDashboard.lua)")
    parser.add_argument("--server-url", help="Dashboard ingestion endpoint URL")
    parser.add_argument("--poll-interval", type=float, help="Seconds between checks for new events")
    parser.add_argument("--ingestion-token", help="Shared token, if the server has INGESTION_TOKEN set")
    parser.add_argument("--tray", action="store_true",
                        help="Run in the system tray instead of this console (always on for the packaged .exe)")
    parser.add_argument("--run-seconds", type=float, help=argparse.SUPPRESS)  # test hook: quit cleanly after N seconds
    return parser.parse_args()


def load_config(args, interactive):
    config = {
        "savedvars_path": None,
        # IP literal, not "localhost": on Windows, resolving "localhost" can
        # try IPv6 first and stall ~2s per new connection before falling
        # back to IPv4. Point this at the dashboard host's real LAN IP when
        # running the watcher from a different machine.
        "server_url": "http://127.0.0.1:5000/api/log-update",
        "poll_interval": 5,
        # Only needed once the server has INGESTION_TOKEN set (e.g. a
        # public/Coolify deployment) — leave unset for a LAN-only server.
        "ingestion_token": None,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                config.update(json.load(f))
        except (OSError, ValueError) as e:
            # A hand-edited config with a stray comma shouldn't surface as a
            # raw traceback (or, in tray mode, as nothing at all).
            raise ConfigError(
                f"Couldn't read {CONFIG_PATH}:\n{e}\n\n"
                "Fix or delete that file and start the watcher again."
            ) from e

    if args.savedvars_path:
        config["savedvars_path"] = args.savedvars_path
    if args.server_url:
        config["server_url"] = args.server_url
    if args.poll_interval:
        config["poll_interval"] = args.poll_interval
    if args.ingestion_token:
        config["ingestion_token"] = args.ingestion_token

    # Tray mode has no console to ask questions on, so it leaves an unset
    # path alone and lets watch() keep looking for it.
    if not config["savedvars_path"] and interactive:
        # No explicit path from a flag or config file — try to find it
        # automatically before asking a (possibly non-technical) friend to
        # hunt one down by hand. Only fires when nothing else specified a
        # path, so anyone with an existing config file/flag is unaffected.
        print("[savedvars-watcher] No SavedVariables path configured — scanning common WoW install locations...")
        found = discover_savedvars_paths()
        if len(found) == 1:
            config["savedvars_path"] = found[0]
            print(f"[savedvars-watcher] Found it automatically: {found[0]}")
        elif len(found) > 1:
            print("[savedvars-watcher] Found more than one — probably multiple WoW installs or accounts. Pick one:")
            for i, path in enumerate(found, 1):
                print(f"  {i}. {path}")
            while True:
                choice = input("Enter a number: ").strip()
                if choice.isdigit() and 1 <= int(choice) <= len(found):
                    config["savedvars_path"] = found[int(choice) - 1]
                    break
                print("Not a valid choice — try again.")
        else:
            print("[savedvars-watcher] Couldn't find it automatically.")
            manual = input(
                "Paste the full path to LanDashboard.lua (under .../WTF/Account/<ACCOUNT NAME>/SavedVariables/) "
                "and press Enter, or leave blank to see example locations: "
            ).strip().strip('"')
            if manual:
                config["savedvars_path"] = manual

        if not config["savedvars_path"]:
            raise ConfigError(
                "No SavedVariables path configured. Pass --savedvars-path, or create "
                'savedvars_watcher_config.json with {"savedvars_path": '
                '"C:\\\\...\\\\WTF\\\\Account\\\\YOURACCOUNT\\\\SavedVariables\\\\LanDashboard.lua"}.\n'
                "Find yours under your WoW install folder — the exact subfolder name depends on\n"
                "which version/client you're running, e.g.:\n"
                r"  <WoW Install>\_classic_era_\WTF\Account\<ACCOUNT NAME>\SavedVariables\LanDashboard.lua" "\n"
                r"  <WoW Install>\_classic_beta_\WTF\Account\<ACCOUNT NAME>\SavedVariables\LanDashboard.lua" "\n"
                r"  <WoW Install>\_retail_\WTF\Account\<ACCOUNT NAME>\SavedVariables\LanDashboard.lua" "\n"
                "Check which folder actually exists under your install rather than assuming — it's\n"
                "easy to copy the wrong example and end up watching a path that never gets created.\n"
                "(this file only appears after the addon has saved at least once — log in, then /reload.)"
            )
    return config


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"sent_count": 0}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def unescape_lua_string(value):
    return value.replace('\\"', '"').replace("\\\\", "\\")


def parse_events(file_text):
    """Extract the ordered list of event strings from the
    `["events"] = { ... }` block. Returns [] if the block can't be found
    (e.g. the file was caught mid-write, or the addon hasn't queued
    anything yet)."""
    start = file_text.find('["events"]')
    if start == -1:
        return []
    brace_start = file_text.find("{", start)
    if brace_start == -1:
        return []

    # Depth-aware match for the closing brace — cheap insurance against a
    # value ever containing a literal brace, even though ours never do.
    depth = 0
    end = None
    for i in range(brace_start, len(file_text)):
        if file_text[i] == "{":
            depth += 1
        elif file_text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        return []

    block = file_text[brace_start:end]
    return [unescape_lua_string(m.group(1)) for m in STRING_LITERAL_RE.finditer(block)]


def is_well_formed(payload):
    parts = payload.split(",")
    if len(parts) < 2:
        return False
    log_type = parts[1]
    expected_min_fields = {"ZONE": 3, "PROFILE": 4, "XP": 5, "QUEST": 4}
    minimum = expected_min_fields.get(log_type)
    if minimum is None:
        return False
    return len(parts) >= minimum


def backup_to_csv(payload):
    timestamp = datetime.now().isoformat()
    with open(BACKUP_CSV, "a", encoding="utf-8") as f:
        f.write(f"{timestamp},{payload}\n")


SESSION = requests.Session()


class ServerRejected(Exception):
    """The server responded 200 OK but rejected the payload (e.g. failed
    validation) — distinct from a connection failure, since retrying a
    payload the server has already rejected will never succeed."""


class AuthFailed(Exception):
    """The server refused the ingestion token. Unlike ServerRejected this is
    about the watcher's setup, not one bad payload, so events must be kept
    and retried once the token is fixed — not skipped."""


def send_packet(server_url, payload, ingestion_token=None, timeout=2.0):
    body = {"timestamp": datetime.now().isoformat(), "data": payload}
    headers = {"X-Ingestion-Token": ingestion_token} if ingestion_token else {}
    response = SESSION.post(server_url, json=body, headers=headers, timeout=timeout)
    if response.status_code in (401, 403):
        raise AuthFailed(f"HTTP {response.status_code}")
    response.raise_for_status()
    # main.py always returns 200 even when it rejects a payload for failing
    # validation — raise_for_status() alone would silently treat that as
    # success, so the body's own status field has to be checked too.
    result = response.json()
    if result.get("status") == "error":
        raise ServerRejected(result.get("detail", "unknown reason"))


def watch(config, stop_event):
    """The polling loop. Runs until stop_event is set (console mode never
    sets it — Ctrl+C ends the process; the tray's Quit does)."""
    state = load_state()
    path = config["savedvars_path"]
    poll = config["poll_interval"]

    say(f"Forwarding to {config['server_url']}")
    if state["sent_count"]:
        say(f"Resuming — {state['sent_count']} events already forwarded in a previous run")
    if path:
        say(f"Watching {path}")
        if not os.path.exists(path):
            say(f"WARNING: this path does not exist yet: {path}")
            say("It appears after the addon saves at least once (log in, then /reload). Waiting...")

    server_down = False
    announced_pending = 0
    # Events before this index are already in the backup CSV. Without it the
    # same still-pending events would be re-appended on every poll while the
    # server is unreachable or refusing — thousands of duplicate lines.
    backed_up_count = state["sent_count"]
    file_was_missing = bool(path) and not os.path.exists(path)
    last_delivery = None
    next_scan = 0.0
    announced_waiting = False

    while not stop_event.is_set():
        if not path:
            # Tray mode: nobody to ask, so keep scanning.
            if time.monotonic() >= next_scan:
                next_scan = time.monotonic() + DISCOVERY_SCAN_SECONDS
                found = discover_savedvars_paths()
                if found:
                    path = most_recently_used(found)
                    say(f"Found it automatically: {path}")
                    if len(found) > 1:
                        say(f"{len(found)} WoW accounts found — watching the most recently used one.")
                    say(f"Watching {path}")
                    file_was_missing = False
            if not path:
                if not announced_waiting:
                    say("No addon save file found yet. It appears after the addon saves once "
                        "(log in, click Sync or type /reload). Waiting...")
                    announced_waiting = True
                set_status("Waiting for the addon's first save", "wait")
                stop_event.wait(poll)
                continue

        if not os.path.exists(path):
            if not file_was_missing:
                log_error(f"SavedVariables file disappeared: {path}")
                file_was_missing = True
            set_status("Waiting for the addon's first save", "wait")
            stop_event.wait(poll)
            continue
        if file_was_missing:
            say(f"Found it — {path} now exists.")
            file_was_missing = False

        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            log_error(f"Could not read SavedVariables file: {e}")
            stop_event.wait(poll)
            continue

        events = parse_events(text)

        # If the file has fewer events than we've supposedly already
        # sent, the addon's queue was reset (e.g. SavedVariables wiped
        # for testing) — start over rather than sending nothing forever.
        if len(events) < state["sent_count"]:
            log_error(f"Event count went backwards ({len(events)} < {state['sent_count']}) "
                      "— assuming the queue was reset.")
            state["sent_count"] = 0
            backed_up_count = 0

        for payload in events[backed_up_count:]:
            backup_to_csv(payload)
        backed_up_count = max(backed_up_count, len(events))

        new_events = events[state["sent_count"]:]

        # Announce only when the pending count changes — while the server
        # is refusing or unreachable the same events stay pending, and
        # repeating this every poll just buries the actual error.
        if len(new_events) != announced_pending:
            if new_events:
                say(f"{len(new_events)} new event(s) queued for delivery")
            announced_pending = len(new_events)

        delivered = 0
        while state["sent_count"] < len(events):
            payload = events[state["sent_count"]]
            if not is_well_formed(payload):
                log_error(f"Skipping malformed queued payload: {payload!r}")
                state["sent_count"] += 1
                save_state(state)
                continue
            try:
                send_packet(config["server_url"], payload, config["ingestion_token"])
                state["sent_count"] += 1
                delivered += 1
                save_state(state)
                if server_down:
                    say("Server connection restored.")
                    notify("Reconnected to the dashboard.")
                    server_down = False
            except ServerRejected as e:
                # Retrying won't help — the server has already told us
                # why this specific payload is invalid. Log it clearly
                # and move past it rather than blocking everything
                # behind it forever.
                log_error(f"Server rejected payload {payload!r}: {e}")
                state["sent_count"] += 1
                save_state(state)
            except AuthFailed as e:
                if not server_down:
                    log_error(
                        f"The server refused the access token ({e}). Check that ingestion_token in "
                        f"{os.path.basename(CONFIG_PATH)} matches the one you were given. "
                        "Events are kept and will be sent once it's fixed."
                    )
                    notify("The dashboard refused your access token. Check savedvars_watcher_config.json.")
                    server_down = True
                set_status("Access token refused — see the log", "error")
                break
            except requests.exceptions.RequestException as e:
                if not server_down:
                    log_error(f"Could not reach the server ({type(e).__name__}) "
                              "— will retry remaining events next poll.")
                    notify("Can't reach the dashboard. Will keep retrying.")
                    server_down = True
                set_status("Can't reach the server — retrying", "wait")
                break

        if delivered:
            last_delivery = datetime.now().strftime("%H:%M")
        if not server_down:
            if last_delivery:
                set_status(f"Connected — last sent {last_delivery}", "ok")
            else:
                set_status("Connected — waiting for new events", "ok")

        stop_event.wait(poll)


def run_console(config):
    stop_event = threading.Event()
    try:
        watch(config, stop_event)
    except KeyboardInterrupt:
        print("\n[savedvars-watcher] Stopped.")


# --- System tray mode --------------------------------------------------------

def show_message(text, error=False):
    """A native message box, for the moments tray mode has nowhere else to
    say something (bad config, already running). Falls back to the console."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "LAN Dashboard Watcher", 0x10 if error else 0x40)
            return
        except Exception:
            pass
    if sys.stdout is not None:
        print(text)


_mutex_handle = None  # kept referenced so the mutex lives as long as the process


def acquire_single_instance():
    """Two watchers on one state file would double-send everything, and with
    no window a second double-click gives no hint the first is running."""
    global _mutex_handle
    if os.name != "nt":
        return True
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _mutex_handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    ERROR_ALREADY_EXISTS = 183
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


class TrayUI:
    def __init__(self, dashboard_url, stop_event):
        # Imported here so console use doesn't need these packages installed.
        import pystray
        from PIL import Image, ImageDraw, ImageFont
        self._Image, self._ImageDraw, self._ImageFont = Image, ImageDraw, ImageFont
        self.stop_event = stop_event
        self.dashboard_url = dashboard_url
        self.status_text = "Starting..."
        self.level = "wait"
        self.images = {level: self._draw_icon(level) for level in ("ok", "wait", "error")}
        menu = pystray.Menu(
            pystray.MenuItem(lambda _item: self.status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", self._open_dashboard, default=True),
            pystray.MenuItem("Open log file", self._open_log),
            pystray.MenuItem("Open watcher folder", self._open_folder),
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon("lan_dashboard_watcher", self.images["wait"], "LAN Dashboard Watcher", menu)

    def _draw_icon(self, level):
        size = 64
        image = self._Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = self._ImageDraw.Draw(image)
        gold = (243, 167, 60, 255)
        draw.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(30, 30, 36, 255), outline=gold, width=3)
        try:
            font = self._ImageFont.truetype("segoeuib.ttf", 28)
            draw.text((size / 2, size / 2 - 3), "4L", font=font, fill=gold, anchor="mm")
        except (OSError, ValueError):
            draw.text((size / 2 - 8, size / 2 - 6), "4L", fill=gold)
        colour = {"ok": (70, 200, 100), "wait": (240, 180, 40), "error": (225, 65, 65)}[level]
        draw.ellipse((36, 36, 60, 60), fill=colour + (255,), outline=(20, 20, 24, 255), width=3)
        return image

    def set_status(self, text, level):
        if text == self.status_text and level == self.level:
            return
        self.status_text, self.level = text, level
        try:
            self.icon.icon = self.images[level]
            self.icon.title = f"LAN Dashboard Watcher - {text}"[:127]  # Windows tooltip limit
            self.icon.update_menu()
        except Exception:
            pass  # cosmetic; never let it take the watcher down

    def notify(self, message):
        with contextlib.suppress(Exception):
            self.icon.notify(message, "LAN Dashboard Watcher")

    def _open_dashboard(self, *_pystray_args):
        webbrowser.open(self.dashboard_url)

    def _open_log(self, *_pystray_args):
        if not os.path.exists(LOG_PATH):
            _append(LOG_PATH, "")
        os.startfile(LOG_PATH)

    def _open_folder(self, *_pystray_args):
        os.startfile(SCRIPT_DIR)

    def _quit(self, *_pystray_args):
        self.stop_event.set()
        self.icon.stop()

    def run(self, worker, run_seconds=None):
        def setup(icon):
            icon.visible = True
            threading.Thread(target=self._work, args=(worker,), daemon=True).start()
            if run_seconds:
                threading.Timer(run_seconds, self._quit).start()
        self.icon.run(setup=setup)

    def _work(self, worker):
        try:
            worker()
        except Exception:
            # Keep the icon up so the player can open the log or quit,
            # instead of the watcher silently vanishing.
            log_error("The watcher stopped after an unexpected error:\n" + traceback.format_exc())
            self.set_status("Stopped after an error — see the log", "error")
            self.notify("The watcher stopped after an error. Open the log from the tray menu.")
        else:
            self.icon.stop()


def run_tray(config, args):
    global TRAY
    stop_event = threading.Event()
    dashboard_url = config["server_url"].split("/api/", 1)[0]
    try:
        TRAY = TrayUI(dashboard_url, stop_event)
    except ImportError as e:
        show_message(f"The tray libraries are missing ({e}). "
                     "Install them with: pip install -r requirements-watcher.txt", error=True)
        return
    say("Started in the system tray.")
    TRAY.run(lambda: watch(config, stop_event), run_seconds=args.run_seconds)


def trim_log():
    try:
        if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            open(LOG_PATH, "w").close()
    except OSError:
        pass


def main():
    args = parse_args()
    tray_mode = args.tray or getattr(sys, "frozen", False)
    trim_log()

    if not tray_mode:
        try:
            config = load_config(args, interactive=True)
        except ConfigError as e:
            raise SystemExit(str(e)) from e
        run_console(config)
        return

    if not acquire_single_instance():
        show_message("LAN Dashboard Watcher is already running.\n\n"
                     "Look for its icon in the system tray (bottom-right of the taskbar "
                     "— it may be under the ^ arrow).")
        return
    try:
        config = load_config(args, interactive=False)
        run_tray(config, args)
    except ConfigError as e:
        show_message(str(e), error=True)
    except Exception:
        log_error("Fatal error:\n" + traceback.format_exc())
        show_message("The watcher hit an unexpected error and had to close. "
                     "Details are in savedvars_watcher.log next to the program.", error=True)


if __name__ == "__main__":
    main()
