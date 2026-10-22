"""Pre-deploy gate: tests, lint, uncommitted work, cache-busting, prod versus local."""
import hashlib
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
MIRRORS = [ROOT.parent / "4L_addon", ROOT.parent / "lan_dashboard_watcher"]
PAGES = ["index.html", "roster.html", "analytics.html", "setup.html", "login.html"]
# Findings that predate the lint config, across main.py, savedvars_watcher.py
# and tests/ (counted 2026-10-22). Lower this as they get fixed; a count above
# it is new work in the diff.
KNOWN_LINT = 13
results = []


def report(level, what, detail=""):
    results.append((level, what, detail))


def run(cmd, cwd=ROOT):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def dotenv():
    values = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip().strip('"')
    return values


def check_tests():
    out = run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", "-q"])
    last = (out.stdout.strip().splitlines() or ["no output"])[-1]
    report("OK" if out.returncode == 0 else "FAIL", "tests", last)


def check_lint():
    out = run([sys.executable, "-m", "ruff", "check", "main.py", "savedvars_watcher.py", "tests"])
    found = re.search(r"Found (\d+) error", out.stdout)
    count = int(found.group(1)) if found else 0
    if count <= KNOWN_LINT:
        report("OK", "lint", f"{count} finding(s), all pre-existing" if count else "clean")
    else:
        # Only the headline lines; the full report is one `ruff check` away.
        heads = [line for line in out.stdout.splitlines() if re.match(r"^[A-Z]+\d+ ", line) or line.startswith("   --> ")]
        report("WARN", "lint", f"{count} finding(s), {count - KNOWN_LINT} more than the known baseline:\n"
               + "\n".join(heads[:30]))


def check_uncommitted():
    for repo in [ROOT, *MIRRORS]:
        if not (repo / ".git").exists():
            report("WARN", f"{repo.name} git", "not a git repository")
            continue
        status = run(["git", "status", "--short"], cwd=repo).stdout.strip()
        ahead = run(["git", "log", "--oneline", "@{u}..HEAD"], cwd=repo).stdout.strip()
        if status:
            report("WARN", f"{repo.name} uncommitted", "\n" + status)
        elif ahead:
            report("WARN", f"{repo.name} unpushed", f"{len(ahead.splitlines())} commit(s) not on the remote")
        else:
            report("OK", f"{repo.name} git", "clean and pushed")


def check_cache_busting():
    changed = set(run(["git", "diff", "HEAD", "--name-only"]).stdout.split())
    # Also anything committed but not yet deployed: compare with the remote branch when it exists.
    upstream = run(["git", "diff", "@{u}", "--name-only"]).stdout.split()
    changed |= set(upstream)
    for asset in ("static/theme.css", "static/dashboard.js"):
        if asset not in changed:
            continue
        name = Path(asset).name
        versions = set()
        for page in PAGES:
            for m in re.finditer(rf'/static/{re.escape(name)}\?v=(\w+)', (ROOT / page).read_text(encoding="utf-8")):
                versions.add(m.group(1))
        head_versions = set()
        for page in PAGES:
            old = run(["git", "show", f"@{{u}}:{page}"]).stdout
            head_versions |= set(re.findall(rf'/static/{re.escape(name)}\?v=(\w+)', old))
        if versions and versions != head_versions:
            report("OK", f"{name} cache-busting", f"version bumped to {', '.join(sorted(versions))}")
        else:
            report("FAIL", f"{name} cache-busting",
                   f"{asset} changed but the ?v= on the pages did not; players would keep the old file")


def check_prod():
    prod = dotenv().get("PROD_URL") or "https://4l.noftz.net"
    try:
        # Cloudflare answers 403 to Python's default User-Agent; name ourselves.
        req = urllib.request.Request(f"{prod}/api/setup-info", headers={"User-Agent": "lan-dashboard-tools/1.0"})
        info = json.load(urllib.request.urlopen(req, timeout=10))
    except Exception as e:
        report("WARN", "prod", f"could not read {prod}/api/setup-info ({e})")
        return
    toc = (ROOT / "LanDashboard" / "LanDashboard.toc").read_text(encoding="utf-8")
    local_addon = re.search(r"^## Version: (.+)$", toc, re.M).group(1).strip()
    prod_addon = (info.get("addon") or {}).get("version")
    report("OK" if prod_addon == local_addon else "WARN", "addon on prod",
           f"prod {prod_addon}, here {local_addon}" + ("" if prod_addon == local_addon else " (redeploy carries it)"))
    exe = ROOT / "downloads" / "savedvars_watcher.exe"
    if exe.exists():
        local_sha = hashlib.sha256(exe.read_bytes()).hexdigest()
        prod_sha = (info.get("watcher") or {}).get("sha256")
        report("OK" if prod_sha == local_sha else "WARN", "watcher exe on prod",
               "same build as downloads/" if prod_sha == local_sha
               else f"prod serves a build from {(info.get('watcher') or {}).get('built')}; upload downloads/savedvars_watcher.exe to /app/downloads")
    else:
        report("WARN", "watcher exe", "no downloads/savedvars_watcher.exe here; run the build-watcher skill")


def main():
    for check in (check_tests, check_lint, check_uncommitted, check_cache_busting, check_prod):
        try:
            check()
        except Exception as e:
            report("WARN", check.__name__, f"check itself failed: {e}")
    width = max(len(w) for _, w, _ in results)
    for level, what, detail in results:
        print(f"{level:4} {what:<{width}}  {detail}")
    worst = "FAIL" if any(l == "FAIL" for l, _, _ in results) else "WARN" if any(l == "WARN" for l, _, _ in results) else "OK"
    print(f"\nOverall: {worst}")
    sys.exit(1 if worst == "FAIL" else 0)


if __name__ == "__main__":
    main()
