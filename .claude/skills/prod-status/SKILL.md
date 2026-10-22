---
name: prod-status
description: Read the live dashboard's state in one shot - who is on the board, which addon and watcher builds are in use, what the server accepted or refused recently - without opening a browser. Use this whenever the user asks how prod is doing, whether a player is showing up, who is on an old build, what the Traffic tab says, whether a sync arrived, or anything about the live server during the stress test or the LAN; and use it before guessing at a cause for "my character isn't on the dashboard". Public endpoints need nothing; the traffic log needs the operator password from data/prod.env, which is git-ignored.
---

# Prod status

```
.venv\Scripts\python.exe .claude/skills/prod-status/scripts/prod_status.py [--traffic 30]
```

Reads `data/prod.env` for `PROD_URL`, `ROSTER_USERNAME` and `ROSTER_PASSWORD`. The URL alone gives the public picture; the credentials add the Traffic tab. Never put the password anywhere else, and never echo it back to the user.

## What it shows and how to read it

- **Builds served.** The addon version the setup page zips and the date of the watcher exe on the downloads mount. If the exe date is older than the last `build-watcher` run, the upload was missed.
- **Leaderboard.** Every visible character with class, level, zone and minutes since the last accepted event. A character with no class has sent XP or zone lines but no PROFILE: that is a login that happened before the current database existed, or an addon problem; a fresh login and `/reload` fixes the first.
- **Traffic** (with credentials). Accepted and refused counts since the container last started, then one line per watcher install: short id, the characters it sends, and the watcher and addon versions it reports. No version means a watcher older than 1.1.0, which also means it will not follow the game's folder on its own. Refused lines are printed in full; a refused event is gone for good, so each one is worth a look.

## Caveats worth repeating to the user

- The traffic log is in memory and starts empty at every container restart. "Nothing from X" right after a redeploy means nothing yet, not nothing ever.
- The stale cutoff for "idle" comes from the server's `STALE_AFTER_MINUTES`; the script shows raw minutes so you can judge for yourself.
