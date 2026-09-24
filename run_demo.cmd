@echo off
setlocal
REM Runs the dashboard against the seeded demo data, on its own port.
REM
REM A .cmd rather than a .ps1 because this machine's execution policy blocks
REM unsigned PowerShell scripts -- a script you cannot run is not a tool.
REM
REM Deliberately a second instance rather than a swap: your real server on
REM :5000 and lan_progression.db are untouched, and nothing here can reach
REM production. Ctrl+C or close the window to stop it.
REM
REM Regenerate the data:  .venv\Scripts\python.exe seed_demo.py
REM (start this server first -- the seeder posts through the normal ingest
REM  API, so the data goes through the same validation real events do)

cd /d "%~dp0"

set "PORT=5001"
set "DB_FILE=%~dp0demo_lan.db"
set "ROSTER_USERNAME=admin"
set "ROSTER_PASSWORD=demo"
REM Cleared so the seeder can post without a token and the setup page's
REM download isn't gated while you're only looking at charts.
set "INGESTION_TOKEN="
set "DOWNLOAD_PASSPHRASE="

REM --- checks, each with its own message ------------------------------------
REM Without these, a double-clicked window closes the instant anything is
REM wrong and shows you nothing at all -- which is exactly what happened when
REM the port was already taken.

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   ERROR: .venv\Scripts\python.exe not found.
    echo   Run this from the project folder, with the virtualenv created.
    echo.
    pause
    exit /b 1
)

REM Not an error: the server creates and migrates the database on startup, so
REM starting without one is how you rebuild it. The earlier version refused
REM here, which blocked the exact workflow its own message recommended.
if not exist "demo_lan.db" (
    echo.
    echo   No demo_lan.db yet - it will be created empty.
    echo   Seed it once this is running:  .venv\Scripts\python.exe seed_demo.py
    echo.
)

netstat -ano -p tcp | findstr /r /c:"LISTENING" | findstr /c:"127.0.0.1:%PORT% " >nul
if not errorlevel 1 (
    echo.
    echo   Port %PORT% is already in use -- something is serving on it.
    echo   Close that window, or edit PORT at the top of this file.
    echo.
    pause
    exit /b 1
)

echo.
echo   Demo dashboard   http://127.0.0.1:%PORT%/
echo   Analytics        http://127.0.0.1:%PORT%/analytics     ^(admin / demo^)
echo.
echo   Data: demo_lan.db - a seeded LAN weekend, 7 characters across 51 hours.
echo   Your real server on :5000 and lan_progression.db are untouched.
echo.

".venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port %PORT%

REM Reached when uvicorn exits, including when it fails to start. Holds the
REM window open so the reason stays on screen.
echo.
echo   Server stopped.
pause
