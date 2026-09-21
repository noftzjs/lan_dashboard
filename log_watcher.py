"""
Tails the WoW chat log file for lines emitted by the LanDashboard addon and
forwards them to the FastAPI server. This is the missing link between the
addon (which only prints to the in-game chat frame) and main.py.

Requires WoW chat logging to be enabled in-game first:
    /console logging chat 1
which makes the client write everything printed to chat (including the
addon's print() calls) into a text file under WTF/../Logs/.

Usage:
    python log_watcher.py --log-path "C:\\Path\\To\\WTF\\...\\Logs\\WoWChatLog.txt"

Settings can also be stored in watcher_config.json next to this script so
players don't need to pass flags every LAN:
    {
      "log_path": "C:\\...\\Logs\\WoWChatLog.txt",
      "server_url": "http://localhost:5000/api/log-update",
      "poll_interval": 0.5
    }
CLI flags override the config file.
"""
import argparse
import collections
import json
import os
import sys
import time
from datetime import datetime

import requests

# Windows terminals default to the legacy cp1252 code page, which can't
# encode arbitrary unicode player/guild names that might get echoed into
# error messages below. Force UTF-8 output where supported (Python 3.7+).
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# A PyInstaller-frozen .exe extracts to a temp dir and __file__ resolves
# there, not next to the .exe — anything written by SCRIPT_DIR path would
# vanish when the process exits. sys.executable is the actual .exe path
# in that case, so config/state/backup files land next to it instead,
# where a player can actually find them between runs.
SCRIPT_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "watcher_config.json")
STATE_PATH = os.path.join(SCRIPT_DIR, ".watcher_state.json")
BACKUP_CSV = os.path.join(SCRIPT_DIR, "watcher_backup.csv")
ERROR_LOG = os.path.join(SCRIPT_DIR, "watcher_errors.log")
TAG = "[DASHBOARD]"
MAX_QUEUE = 5000


def load_config():
    config = {
        "log_path": None,
        # IP literal, not "localhost": on Windows, resolving "localhost" can
        # try IPv6 first and stall ~2s per new connection before falling
        # back to IPv4. Point this at the dashboard host's real LAN IP when
        # running the watcher from a different machine.
        "server_url": "http://127.0.0.1:5000/api/log-update",
        "poll_interval": 0.5,
        # Only needed once the server has INGESTION_TOKEN set (e.g. a
        # public/Coolify deployment) — leave unset for a LAN-only server.
        "ingestion_token": None,
    }
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            config.update(json.load(f))

    parser = argparse.ArgumentParser(description="Tail WoW chat log and forward LAN dashboard events.")
    parser.add_argument("--log-path", help="Path to the WoW chat log file (e.g. .../Logs/WoWChatLog.txt)")
    parser.add_argument("--server-url", help="Dashboard ingestion endpoint URL")
    parser.add_argument("--poll-interval", type=float, help="Seconds between log file checks")
    parser.add_argument("--ingestion-token", help="Shared token, if the server has INGESTION_TOKEN set")
    args = parser.parse_args()

    if args.log_path:
        config["log_path"] = args.log_path
    if args.server_url:
        config["server_url"] = args.server_url
    if args.poll_interval:
        config["poll_interval"] = args.poll_interval
    if args.ingestion_token:
        config["ingestion_token"] = args.ingestion_token

    if not config["log_path"]:
        raise SystemExit(
            "No log file path configured. Pass --log-path, or create watcher_config.json "
            'with {"log_path": "C:\\\\...\\\\Logs\\\\WoWChatLog.txt"}.\n'
            "Typical locations to check once chat logging is enabled — the exact subfolder\n"
            "depends on which version/client you're running, so check what actually exists\n"
            "under your install rather than assuming:\n"
            r"  <WoW Install>\_classic_era_\Logs\WoWChatLog.txt" "\n"
            r"  <WoW Install>\_classic_beta_\Logs\WoWChatLog.txt" "\n"
            r"  <WoW Install>\_retail_\Logs\WoWChatLog.txt"
        )
    return config


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


def log_error(message):
    timestamp = datetime.now().isoformat()
    with open(ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(f"{timestamp}\t{message}\n")
    print(f"[watcher] {message}")


def extract_payload(line):
    """Pull the CSV payload out of a raw chat log line, ignoring whatever
    timestamp/channel prefix WoW's logger adds before the addon's tag."""
    idx = line.find(TAG)
    if idx == -1:
        return None
    payload = line[idx + len(TAG):].strip()
    return payload or None


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


def send_packet(server_url, payload, ingestion_token=None, timeout=1.0):
    body = {"timestamp": datetime.now().isoformat(), "data": payload}
    headers = {"X-Ingestion-Token": ingestion_token} if ingestion_token else {}
    response = SESSION.post(server_url, json=body, headers=headers, timeout=timeout)
    response.raise_for_status()
    # main.py always returns 200 even when it rejects a payload for failing
    # validation — raise_for_status() alone would silently treat that as
    # success, so the body's own status field has to be checked too.
    result = response.json()
    if result.get("status") == "error":
        raise ServerRejected(result.get("detail", "unknown reason"))


def follow(log_path, poll_interval, state):
    """Yields new lines appended to log_path, handling truncation/rotation
    (WoW starts a fresh log each session)."""
    last_pos = state.get(log_path, 0)
    file_handle = None
    was_connected = False

    while True:
        if not os.path.exists(log_path):
            if was_connected:
                log_error(f"Log file disappeared: {log_path}")
                was_connected = False
            time.sleep(poll_interval)
            continue

        if file_handle is None:
            file_handle = open(log_path, "r", encoding="utf-8", errors="replace")
            size = os.path.getsize(log_path)
            # Resume where we left off unless the file is smaller than that
            # (new session / rotated file) in which case start from the top.
            file_handle.seek(last_pos if last_pos <= size else 0)
            if not was_connected:
                print(f"[watcher] Watching {log_path}")
                was_connected = True

        current_size = os.path.getsize(log_path)
        if current_size < file_handle.tell():
            file_handle.close()
            file_handle = None
            continue

        line = file_handle.readline()
        if not line:
            last_pos = file_handle.tell()
            state[log_path] = last_pos
            time.sleep(poll_interval)
            continue

        yield line.rstrip("\n")
        last_pos = file_handle.tell()
        state[log_path] = last_pos


def run():
    config = load_config()
    state = load_state()
    retry_queue = collections.deque(maxlen=MAX_QUEUE)
    server_down = False

    print(f"[watcher] Forwarding to {config['server_url']}")
    print(f"[watcher] Backing up every event to {BACKUP_CSV}")

    try:
        for line in follow(config["log_path"], config["poll_interval"], state):
            save_state(state)

            payload = extract_payload(line)
            if payload is None:
                continue

            if not is_well_formed(payload):
                log_error(f"Skipping malformed payload: {payload!r}")
                continue

            backup_to_csv(payload)
            retry_queue.append(payload)

            while retry_queue:
                pending = retry_queue[0]
                try:
                    send_packet(config["server_url"], pending, config["ingestion_token"])
                    retry_queue.popleft()
                    if server_down:
                        print("[watcher] Server connection restored.")
                        server_down = False
                except ServerRejected as e:
                    # Retrying won't help — the server has already told us
                    # why this specific payload is invalid.
                    log_error(f"Server rejected payload {pending!r}: {e}")
                    retry_queue.popleft()
                except requests.exceptions.RequestException:
                    if not server_down:
                        log_error("Server unreachable, queuing events until it returns.")
                        server_down = True
                    break
    except KeyboardInterrupt:
        print("\n[watcher] Stopped.")
        save_state(state)


if __name__ == "__main__":
    run()
