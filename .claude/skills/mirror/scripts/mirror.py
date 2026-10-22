"""Copy the addon and watcher into their public mirror repos and show what changed."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
ADDON_REPO = ROOT.parent / "4L_addon"
WATCHER_REPO = ROOT.parent / "lan_dashboard_watcher"
COPIES = [
    (ROOT / "LanDashboard" / "LanDashboard.lua", ADDON_REPO / "LanDashboard" / "LanDashboard.lua"),
    (ROOT / "LanDashboard" / "LanDashboard.toc", ADDON_REPO / "LanDashboard" / "LanDashboard.toc"),
    (ROOT / "savedvars_watcher.py", WATCHER_REPO / "savedvars_watcher.py"),
    (ROOT / "log_watcher.py", WATCHER_REPO / "log_watcher.py"),
    (ROOT / "requirements-watcher.txt", WATCHER_REPO / "requirements-watcher.txt"),
    (ROOT / "tests" / "test_watcher_startup.py", WATCHER_REPO / "tests" / "test_watcher_startup.py"),
    (ROOT / "tests" / "test_watcher_discovery.py", WATCHER_REPO / "tests" / "test_watcher_discovery.py"),
]


def main():
    for repo in (ADDON_REPO, WATCHER_REPO):
        if not (repo / ".git").exists():
            sys.exit(f"{repo} is not a git repository; the mirrors are expected beside this project.")
    changed = 0
    for src, dst in COPIES:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.read_bytes() != src.read_bytes():
            shutil.copyfile(src, dst)
            changed += 1
            print(f"updated  {dst.relative_to(ROOT.parent)}")
    print(f"{changed} file(s) updated." if changed else "Mirrors already matched.")

    toc = (ROOT / "LanDashboard" / "LanDashboard.toc").read_text(encoding="utf-8")
    addon = re.search(r"^## Version: (.+)$", toc, re.M)
    watcher = re.search(r'^WATCHER_VERSION = "([^"]+)"', (ROOT / "savedvars_watcher.py").read_text(encoding="utf-8"), re.M)
    print(f"\nAddon version {addon.group(1).strip() if addon else '?'}, watcher version {watcher.group(1) if watcher else '?'}")
    for repo in (ADDON_REPO, WATCHER_REPO):
        status = subprocess.run(["git", "status", "--short"], cwd=repo, capture_output=True, text=True, check=False).stdout
        print(f"\n{repo.name}: " + ("clean" if not status.strip() else "uncommitted"))
        if status.strip():
            print(status.rstrip())


if __name__ == "__main__":
    main()
