---
name: build-watcher
description: Build the watcher .exe the right way and prove it is the right build. Use this whenever savedvars_watcher.py changed and a packaged build is needed, whenever the user says "rebuild", "build the watcher", "new exe", or asks to put a watcher build on their machine or on prod, and whenever a watcher bug report might come from an old build. It runs PyInstaller into downloads/, checks the result is a no-console tray build, prints its size and SHA-256, compares it with the build prod is serving, and installs it into the user's own watcher folder when that is safe.
---

# Build the watcher

```
.venv\Scripts\python.exe .claude/skills/build-watcher/scripts/build_watcher.py [--no-install]
```

Why a script: the one time this was done by hand, a September console build from `dist\` got copied instead of the new tray build from `downloads\`, and the player saw a terminal window. The script only ever builds into `downloads\`, and refuses to call a build done unless the exe's Windows subsystem flag says "GUI", which is what `--noconsole` produces.

## What it prints

- The PyInstaller result and the new exe's size and SHA-256.
- Whether prod's `/api/setup-info` reports the same SHA-256. "Different" means the exe still has to be uploaded to the `/app/downloads` mount; the server serves whatever file is there, with no redeploy.
- Whether it installed the build into the folder named by `WATCHER_INSTALL_DIR` in `.env`. It does not install while a watcher is running from that folder, because Windows will not overwrite a running exe; it tells you to quit it from the tray first.

## Build only when the source changed

PyInstaller output is not reproducible: two builds of the same source have different hashes. The `deploy-check` skill tells "uploaded" from "not uploaded" by hash, so a rebuild for no reason makes prod look stale. Build when savedvars_watcher.py changed, not to see whether the build works.

## After a build

- Bump `WATCHER_VERSION` in savedvars_watcher.py before building a release, so the Traffic tab can tell builds apart.
- Run the `mirror` skill so the public watcher repo matches what players can download.
- Tell the user the exe still needs uploading to prod unless the script said the hashes match.
