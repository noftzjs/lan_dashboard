"""Start the demo server, screenshot one page with a fresh headless browser
profile, stop the server. See ../SKILL.md for what each piece is for.

    .venv\\Scripts\\python.exe .claude/skills/preview/scripts/preview.py <page> [--open] [--height N] [--width N]
"""
import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
PORT = 5001
BASE = f"http://127.0.0.1:{PORT}"
PAGES = {"dashboard": "/", "setup": "/setup", "login": "/login", "analytics": "/analytics", "roster": "/roster"}
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]
ADMIN = ("admin", "demo")


def port_in_use():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def request(path, data=None, auth=None, method=None):
    req = urllib.request.Request(BASE + path, method=method or ("POST" if data is not None else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if auth:
        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    body = json.dumps(data).encode() if data is not None else None
    with urllib.request.urlopen(req, body, timeout=10) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def wait_for_server(seconds=25):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(BASE + "/setup", timeout=2).read()
            return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    return False


def browser():
    for candidate in BROWSERS:
        if os.path.exists(candidate):
            return candidate
    sys.exit("No Chrome or Edge found for the headless screenshot.")


def shoot(exe, profile, url, out, width, height):
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--no-first-run", "--hide-scrollbars",
                    f"--user-data-dir={profile}", f"--window-size={width},{height}",
                    "--virtual-time-budget=6000", f"--screenshot={out}", url],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=90, check=False)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("page", choices=sorted(PAGES))
    parser.add_argument("--open", action="store_true", help="no download passphrase, like a dev server")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=2400)
    args = parser.parse_args()

    if port_in_use():
        sys.exit(f"Something is already listening on port {PORT}. Stop it first; this script only screenshots its own server.")
    db = ROOT / "data" / "demo_lan.db"
    if not db.exists():
        sys.exit(f"{db} is missing. Seed it with tools/seed_demo.py against a running demo server.")

    env = {**os.environ, "PORT": str(PORT), "DB_FILE": str(db), "ROSTER_USERNAME": ADMIN[0],
           "ROSTER_PASSWORD": ADMIN[1], "INGESTION_TOKEN": "demotoken",
           "DOWNLOAD_PASSPHRASE": "" if args.open else "demo-pass"}
    previews = ROOT / "data" / "previews"
    previews.mkdir(parents=True, exist_ok=True)
    log = open(previews / "server.log", "w", encoding="utf-8")
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(PORT)],
                              cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    profile = tempfile.mkdtemp(prefix="preview-profile-")
    token_label = f"preview-{os.getpid()}"
    out = previews / f"{args.page}-{datetime.now().strftime('%H%M%S')}.png"
    try:
        if not wait_for_server():
            sys.exit(f"The demo server did not come up; see {previews / 'server.log'}")
        exe = browser()
        if args.page in ("analytics", "roster"):
            role = "operator" if args.page == "roster" else "viewer"
            link = request("/api/access-tokens", {"label": token_label, "role": role}, auth=ADMIN)["link"]
            # The link sets the session cookie in this profile and redirects; the
            # real screenshot is the second visit.
            shoot(exe, profile, link.replace("http://127.0.0.1:5001", BASE), previews / "login-hop.png", 800, 400)
        shoot(exe, profile, f"{BASE}{PAGES[args.page]}?preview={int(time.time())}", out, args.width, args.height)
        if not out.exists() or out.stat().st_size < 1000:
            sys.exit("The browser produced no screenshot.")
        print(out)
    finally:
        try:
            tokens = request("/api/access-tokens", auth=ADMIN)
            items = tokens if isinstance(tokens, list) else tokens.get("tokens", tokens.get("links", []))
            for t in items:
                if t.get("label") == token_label and not t.get("revoked_at"):
                    request(f"/api/access-tokens/{t['id']}/revoke", {}, auth=ADMIN)
        except Exception:
            pass
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(server.pid), "/T", "/F"], stdout=subprocess.DEVNULL, check=False)
        log.close()
        shutil.rmtree(profile, ignore_errors=True)
        for _ in range(20):
            if not port_in_use():
                break
            time.sleep(0.25)
        else:
            print(f"WARNING: port {PORT} is still in use after stopping the server.", file=sys.stderr)


if __name__ == "__main__":
    main()
