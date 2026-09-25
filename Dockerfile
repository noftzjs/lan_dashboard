# Runs the FastAPI dashboard server (main.py) for Coolify or any other
# Docker host. Does NOT package the watchers — those run on each player's
# own Windows machine as .exe files (see README.md, "Package the Watchers
# for Remote Friends"), never inside this container.
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py index.html roster.html setup.html analytics.html login.html ./
COPY static/ ./static/
# Served by the /setup page: the addon zip is built from LanDashboard/ on each
# request, so the addon ships with the code.
COPY LanDashboard/ ./LanDashboard/

# lan_progression.db (or whatever DB_FILE points at) is created here at
# startup if it doesn't exist. On Coolify, mount a persistent volume at
# /app/data and set DB_FILE=/app/data/lan_progression.db — otherwise every
# redeploy recreates the container filesystem from scratch and silently
# wipes all progression data. See README.md Phase 4 for the full note.
#
# /app/downloads is where the prebuilt watcher .exe is served from. It's a
# 14 MB binary that changes on every rebuild, so it's deliberately not in git
# or in the image: mount a persistent directory here and upload the .exe into
# it. Until it's there, /setup greys out the watcher download and everything
# else keeps working.
RUN mkdir -p /app/data /app/downloads

EXPOSE 5000

CMD ["python", "main.py"]
