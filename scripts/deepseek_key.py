#!/usr/bin/env python3
"""Print a DeepSeek API key for Codex auth without persisting it in config."""

from __future__ import annotations

import getpass
import os
import platform
import subprocess
import sys

import win_cred  # local sibling module; inert outside Windows


SERVICE = "codex-deepseek-api"


def macos_keychain_key() -> str | None:
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
        return None
    return key


def native_key() -> str | None:
    """Return the key from the platform's native credential store, if any."""
    system = platform.system()
    if system == "Darwin":
        return macos_keychain_key()
    if system == "Windows":
        try:
            return win_cred.read_credential(SERVICE)
        except OSError as error:
            print(
                f"Windows Credential Manager is unavailable: {error}",
                file=sys.stderr,
            )
            return None
    return None


def main() -> int:
    # Environment always wins so CI, servers, and Linux can use it directly.
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        print(env_key)
        return 0

    key = native_key()
    if not key:
        print(
            "DeepSeek API key not found. Set DEEPSEEK_API_KEY, or run "
            "this skill's setup.py store-key action.",
            file=sys.stderr,
        )
        return 1

    print(key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
