---
name: preview
description: Render a dashboard page exactly as a player or the operator sees it, as a screenshot, without touching prod. Use this whenever a page's look needs checking after an HTML, CSS or JS change in this project, whenever the user asks to "see", "check", "screenshot" or "preview" the dashboard, setup, login, analytics or roster page, and before declaring any page change done. It starts the demo server on port 5001 against data/demo_lan.db in the same gated mode prod runs (passphrase and token set), logs in through a one-time access link for the operator and viewer pages, screenshots with a fresh headless Chrome profile so no cached stylesheet can fool you, and stops the server again.
---

# Preview a page

One command does the whole dance that used to be done by hand and once showed a stale page from a cached browser profile:

```
.venv\Scripts\python.exe .claude/skills/preview/scripts/preview.py <page> [--open] [--height 2400] [--width 1280]
```

`<page>` is one of `dashboard`, `setup`, `login`, `analytics`, `roster`. The script prints the PNG path under `data/previews/`. Read that PNG and look at it; the screenshot is the deliverable, not the script's exit code.

## What it does, so you know what you are looking at

- Serves `data/demo_lan.db`, a seeded fake LAN weekend, on 127.0.0.1:5001 with `ROSTER_USERNAME=admin`, `ROSTER_PASSWORD=demo`, an ingestion token and a download passphrase set. That is prod's shape: the setup page shows the passphrase form, not the open download. Pass `--open` to see the no-passphrase variant instead.
- For `analytics` and `roster` it mints an access link (viewer or operator), opens it once to set the session cookie, screenshots the page, then revokes the link. The demo database gets a revoked token row each time; that is fine.
- Always a fresh Chrome profile, and the URL carries a cache-busting query, so what you see is what is on disk right now.
- Stops the server and checks port 5001 is free before returning. If something was already listening on 5001 it refuses to start rather than screenshot a stranger.

## Reading the result

- Default height is 2400px, which fits every page at desktop width. Use `--height 900` when you only need the top, or a larger value for a long roster.
- A page that shows "Couldn't load" or an empty table usually means the server was not ready yet; run it again before concluding anything.
- The tray-icon screenshots and the in-game panels are static images in `static/`; they will not change with code.

## When you are done

Delete nothing. The PNGs under `data/previews/` are git-ignored and cheap; the user may want to compare before and after.
