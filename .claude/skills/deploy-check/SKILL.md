---
name: deploy-check
description: The gate before pushing the dashboard to prod. Run it whenever the user is about to deploy, redeploy, push to main, "ship", "go live", or asks whether things are ready, and before telling the user a change is ready to deploy. It runs the test suite and the linter, lists uncommitted work in this repo and the two mirror repos, catches a changed stylesheet or script whose cache-busting version was not bumped (the exact mistake that left players on an old stylesheet), and compares the addon version and watcher build on prod with what is here, so "did I upload the exe" is a yes or no rather than a guess.
---

# Deploy check

```
.venv\Scripts\python.exe .claude/skills/deploy-check/scripts/deploy_check.py
```

Each line is OK, WARN or FAIL with a one-line reason. Relay the WARN and FAIL lines to the user with what to do about each; the OK lines are the point of the script and need no commentary.

## What each check protects

- **Tests and lint.** Thirteen ruff findings across main.py, the watcher and the tests predate this project's lint config and are the known baseline in the script; a count above it is new. When some get fixed, lower `KNOWN_LINT` in the script.
- **Uncommitted work**, here and in `../4L_addon` and `../lan_dashboard_watcher`. The user commits; the check only makes sure nothing is forgotten, especially a mirror that was never pushed after a version bump.
- **Cache-busting.** `static/theme.css` and `static/dashboard.js` are cached by Cloudflare for hours and by browsers after that. Every page links them with a `?v=` query, and that query has to change when the file does, or players keep the old file after a deploy with no way to know.
- **Prod versus here.** `/api/setup-info` on prod reports the addon version it zips and the SHA-256 of the exe it serves. The addon follows a redeploy; the exe does not, it is a file on the `/app/downloads` mount that has to be uploaded by hand.
- **Data cutover reminders** are not checked here; they live in BACKLOG.md under the go-live checklist and in memory.

## When it says WARN about the exe

The build in `downloads\` differs from prod's. Upload it over SFTP to the downloads mount; no redeploy needed. Then run this again and the line turns OK.

One false alarm to know about: PyInstaller output is not byte-for-byte reproducible, so rebuilding from unchanged source also changes the hash. If `build-watcher` was just run as a test rather than for a real change, this WARN only means "a different file", not "a newer watcher". Compare the `WATCHER_VERSION` the Traffic tab shows against the source if in doubt.
