"""Run npm in web/ from a poe task on any OS (npm is npm.cmd on Windows, which poe cannot spawn)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
COMMANDS = {
    "build": [["ci", "--no-audit", "--no-fund"], ["run", "build"]],
    "dev": [["run", "dev"]],
}


def main() -> int:
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action not in COMMANDS:
        print(f"usage: python scripts/web.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    npm = shutil.which("npm")
    if npm is None:
        print("npm not found on PATH; install Node 20.19+ or 22.12+", file=sys.stderr)
        return 2
    for args in COMMANDS[action]:
        code = subprocess.call([npm, *args], cwd=WEB)
        if code != 0:
            return code
    return 0


if __name__ == "__main__":
    sys.exit(main())
