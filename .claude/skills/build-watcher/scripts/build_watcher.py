"""Build the watcher exe into downloads/, verify it, compare with prod, install locally.

    .venv\\Scripts\\python.exe .claude/skills/build-watcher/scripts/build_watcher.py [--no-install]
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EXE = ROOT / "downloads" / "savedvars_watcher.exe"


def dotenv():
    values = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


def subsystem(path):
    data = path.read_bytes()
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    return struct.unpack_from("<H", data, pe + 4 + 20 + 68)[0]   # 2 = GUI, 3 = console


def running_from(folder):
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-Process savedvars_watcher -ErrorAction SilentlyContinue | ForEach-Object { $_.Path }"],
                         capture_output=True, text=True, check=False).stdout
    return [p for p in out.splitlines() if p.strip() and Path(p.strip()).parent.resolve() == Path(folder).resolve()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-install", action="store_true")
    args = parser.parse_args()
    cfg = dotenv()

    source = (ROOT / "savedvars_watcher.py").read_text(encoding="utf-8")
    version = re.search(r'^WATCHER_VERSION = "([^"]+)"', source, re.M)
    print(f"Source version: {version.group(1) if version else 'unknown (no WATCHER_VERSION constant)'}")

    cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole", "--name", "savedvars_watcher",
           "--distpath", "downloads", "--workpath", "build", "--specpath", "build", "savedvars_watcher.py"]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stdout[-3000:], result.stderr[-3000:])
        sys.exit("PyInstaller failed.")
    if subsystem(EXE) != 2:
        sys.exit(f"{EXE} is not a no-console build (subsystem {subsystem(EXE)}). Do not ship it.")
    digest = hashlib.sha256(EXE.read_bytes()).hexdigest()
    print(f"Built {EXE}: {EXE.stat().st_size:,} bytes, GUI subsystem, sha256 {digest}")

    prod = cfg.get("PROD_URL") or "https://4l.noftz.net"
    try:
        # Cloudflare answers 403 to Python's default User-Agent; name ourselves.
        req = urllib.request.Request(f"{prod}/api/setup-info", headers={"User-Agent": "lan-dashboard-tools/1.0"})
        info = json.load(urllib.request.urlopen(req, timeout=10))
        served = (info.get("watcher") or {}).get("sha256")
        if served == digest:
            print(f"Prod ({prod}) already serves this exact build.")
        else:
            print(f"Prod ({prod}) serves a DIFFERENT build (built {(info.get('watcher') or {}).get('built')}). "
                  "Upload downloads/savedvars_watcher.exe to the /app/downloads mount.")
    except Exception as e:
        print(f"Could not compare with prod ({e}).")

    install_dir = cfg.get("WATCHER_INSTALL_DIR")
    if args.no_install or not install_dir:
        print("Not installed locally" + ("" if install_dir else " (set WATCHER_INSTALL_DIR in .env to enable)."))
        return
    if running_from(install_dir):
        print(f"A watcher is running from {install_dir}; quit it from the tray, then copy the exe there "
              "(or run this again).")
        return
    target = Path(install_dir) / "savedvars_watcher.exe"
    shutil.copyfile(EXE, target)
    print(f"Installed to {target}. Start it from there; the old state file carries over.")


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
