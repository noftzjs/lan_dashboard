"""PostToolUse hook: lint the file Claude just wrote, and hand the findings back.

Reads the hook's JSON on stdin, runs ruff on a .py file (luacheck on a .lua
file, if luacheck is installed), and prints a JSON reply whose
additionalContext carries the findings so they surface immediately rather
than at the next manual lint pass. Clean files produce no output at all.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def main():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    path = (payload.get("tool_input") or {}).get("file_path") or (payload.get("tool_response") or {}).get("filePath")
    if not path:
        return
    path = Path(path)
    if not path.exists():
        return
    suffix = path.suffix.lower()
    if suffix == ".py":
        if not PYTHON.exists():
            return
        cmd = [str(PYTHON), "-m", "ruff", "check", "--quiet", "--output-format", "concise", str(path)]
        tool = "ruff"
    elif suffix == ".lua":
        # Project-local binary first (tools/luacheck.exe, git-ignored; see
        # README "Tests and lint"), then whatever is on PATH.
        local = ROOT / "tools" / "luacheck.exe"
        luacheck = str(local) if local.exists() else shutil.which("luacheck")
        if not luacheck:
            return
        cmd = [luacheck, "--no-color", "--codes", str(path)]
        tool = "luacheck"
    else:
        return
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode == 0:
        return
    findings = (result.stdout.strip() or result.stderr.strip()).splitlines()
    shown = "\n".join(findings[:25]) + (f"\n... {len(findings) - 25} more" if len(findings) > 25 else "")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"{tool} on {path.relative_to(ROOT) if ROOT in path.parents else path}:\n{shown}",
        },
        "suppressOutput": True,
    }))


if __name__ == "__main__":
    os.chdir(ROOT)
    main()
