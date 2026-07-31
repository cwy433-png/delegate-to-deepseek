#!/usr/bin/env python3
"""Print a DeepSeek API key for Codex auth without persisting it in config."""

from __future__ import annotations

import getpass
import os
import platform
import subprocess
import sys


SERVICE = "codex-deepseek-api"


def main() -> int:
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        print(env_key)
        return 0

    if platform.system() != "Darwin":
        print(
            "DEEPSEEK_API_KEY is unset and macOS Keychain is unavailable.",
            file=sys.stderr,
        )
        return 1

    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            getpass.getuser(),
            "-s",
            SERVICE,
            "-w",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    key = result.stdout.strip()
    if result.returncode != 0 or not key:
        print(
            "DeepSeek API key not found. Run: "
            "python3 ~/.codex/skills/delegate-to-deepseek/scripts/setup.py store-key",
            file=sys.stderr,
        )
        return 1

    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
