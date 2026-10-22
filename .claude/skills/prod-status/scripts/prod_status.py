"""One-shot view of the live dashboard: builds served, leaderboard, traffic."""
import argparse
import base64
import collections
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def prod_env():
    values = {"PROD_URL": "https://4l.noftz.net"}
    path = ROOT / "data" / "prod.env"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


def get(url, auth=None):
    # Cloudflare answers 403 to Python's default User-Agent; name ourselves.
    req = urllib.request.Request(url, headers={"User-Agent": "lan-dashboard-tools/1.0"})
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode())
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def ago(iso):
    if not iso:
        return "never"
    t = datetime.fromisoformat(iso)
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    minutes = (datetime.now(timezone.utc) - t).total_seconds() / 60
    return f"{minutes / 60:.1f} h" if minutes > 120 else f"{minutes:.0f} min"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--traffic", type=int, default=30, help="how many recent traffic entries to summarise")
    args = parser.parse_args()
    cfg = prod_env()
    base = cfg["PROD_URL"].rstrip("/")
    auth = (cfg.get("ROSTER_USERNAME"), cfg.get("ROSTER_PASSWORD")) if cfg.get("ROSTER_PASSWORD") else None

    info = get(f"{base}/api/setup-info")
    watcher = info.get("watcher") or {}
    print(f"== {base}")
    print(f"Addon served: {(info.get('addon') or {}).get('version')}   "
          f"Watcher exe: built {watcher.get('built', '?')[:10]}, sha256 {str(watcher.get('sha256', '?'))[:12]}   "
          f"stale after {info.get('stale_after_minutes')} min")

    players = get(f"{base}/api/leaderboard")
    print(f"\n== Leaderboard: {len(players)} character(s)")
    for p in sorted(players, key=lambda p: (-p['level'], p['name'])):
        idle = p.get("idle_seconds")
        print(f"  {p['name']:<22} {str(p.get('class') or 'NO PROFILE'):<10} L{p['level']:<3} {str(p.get('current_zone') or '-'):<22}"
              f" last event {idle / 60:.0f} min ago" if idle is not None else
              f"  {p['name']:<22} {str(p.get('class') or 'NO PROFILE'):<10} L{p['level']:<3} {str(p.get('current_zone') or '-'):<22} no events")

    if not auth:
        print("\n(no ROSTER_PASSWORD in data/prod.env, so no traffic log)")
        return
    try:
        traffic = get(f"{base}/api/traffic", auth)
    except urllib.error.HTTPError as e:
        print(f"\nTraffic: HTTP {e.code} (check the credentials in data/prod.env)")
        return
    entries = traffic.get("entries", traffic if isinstance(traffic, list) else [])
    accepted = sum(1 for e in entries if e.get("accepted"))
    print(f"\n== Traffic since the last restart: {len(entries)} entries, {accepted} accepted, {len(entries) - accepted} refused"
          f" (log holds {traffic.get('capacity', '?')})")
    by_watcher = collections.OrderedDict()
    for e in entries:
        key = (e.get("watcher_id") or "no id")[:8]
        w = by_watcher.setdefault(key, {"players": set(), "watcher": None, "addon": None, "last": None, "n": 0})
        w["players"].add(e.get("player") or "?")
        w["watcher"] = e.get("watcher_version") or w["watcher"]
        w["addon"] = e.get("addon_version") or w["addon"]
        w["last"] = e.get("at")
        w["n"] += 1
    for key, w in by_watcher.items():
        print(f"  via {key}: {', '.join(sorted(w['players']))}  watcher {w['watcher'] or 'pre-1.1.0'}  "
              f"addon {w['addon'] or '?'}  {w['n']} events, last {ago(w['last'])} ago")
    refused = [e for e in entries if not e.get("accepted")][-args.traffic:]
    if refused:
        print("\n== Refused (each one is lost for good):")
        for e in refused:
            print(f"  {e['at'][11:19]} {e.get('player')}/{e.get('log_type')}: {e.get('detail')}  <- {e.get('payload')}")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as e:
        sys.exit(f"Could not reach prod: {e}")
