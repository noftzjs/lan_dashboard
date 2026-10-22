---
name: mirror
description: Copy the addon and watcher sources from this dashboard repo into their two public mirror repos (../4L_addon and ../lan_dashboard_watcher) and report what changed there. Use this after any edit to LanDashboard/, savedvars_watcher.py, log_watcher.py, requirements-watcher.txt or the watcher tests, after a version bump of either, before a CurseForge upload, and whenever the user mentions the mirror repos, publishing the addon or watcher code, or "sync the repos". The dashboard repo is the source of truth because its Dockerfile and setup page build the addon zip from its own copy; the mirrors exist so players can read the code, and they drift silently if this step is skipped.
---

# Mirror the addon and watcher

```
.venv\Scripts\python.exe .claude/skills/mirror/scripts/mirror.py
```

The script copies the files, then prints `git status --short` for each mirror plus the addon version from the toc and the watcher's `WATCHER_VERSION`, so the commit message writes itself.

## Rules that make the mirrors trustworthy

- Never edit in a mirror. Fix it here, run this, commit there. A change made in a mirror is lost at the next copy.
- The mirrors carry no history from this repo on purpose; a fresh first commit was chosen so nothing from early history could leak into a public repo. Keep it that way: copy, don't subtree.
- Commits in the mirrors are the user's to make, like everywhere else in this project. Offer the message; do not run `git commit`.
- An addon version bump also means a CurseForge upload of the zip the setup page serves (`/download/LanDashboard.zip`) and a changelog entry written for players, not developers.

## Files that travel

| From here | To |
|---|---|
| `LanDashboard/LanDashboard.lua`, `LanDashboard/LanDashboard.toc` | `../4L_addon/LanDashboard/` |
| `savedvars_watcher.py`, `log_watcher.py`, `requirements-watcher.txt` | `../lan_dashboard_watcher/` |
| `tests/test_watcher_startup.py`, `tests/test_watcher_discovery.py` | `../lan_dashboard_watcher/tests/` |

Tests that need the server (`test_watcher_identity.py`) stay here; the watcher repo's README says so.
