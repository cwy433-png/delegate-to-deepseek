#!/usr/bin/env python3
"""Install the DeepSeek Codex profile and manage its macOS Keychain key."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile


PROFILE_NAME = "deepseek-flash"
SERVICE = "codex-deepseek-api"
MARKER = "# Managed by delegate-to-deepseek"


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def profile_path() -> Path:
    return codex_home() / f"{PROFILE_NAME}.config.toml"


def profile_text() -> str:
    root = skill_dir()
    catalog = root / "assets" / "models.json"
    key_command = root / "scripts" / "deepseek_key.py"
    return f'''{MARKER}
model = "deepseek-v4-flash"
model_provider = "deepseek"
model_catalog_json = "{catalog}"
model_reasoning_effort = "high"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
wire_api = "responses"
request_max_retries = 3
stream_max_retries = 3
stream_idle_timeout_ms = 300000
supports_websockets = false

[model_providers.deepseek.auth]
command = "{key_command}"
timeout_ms = 5000
refresh_interval_ms = 0
'''


def install() -> int:
    target = profile_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    desired = profile_text()
    if target.exists():
        current = target.read_text(encoding="utf-8")
        if current == desired:
            print(f"Profile already current: {target}")
            return 0
        if not current.startswith(MARKER):
            print(
                f"Refusing to overwrite unmanaged profile: {target}",
                file=sys.stderr,
            )
            return 1

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        handle.write(desired)
        temporary = Path(handle.name)
    os.replace(temporary, target)
    target.chmod(0o600)
    print(f"Installed Codex profile: {target}")
    return 0


def store_key() -> int:
    if platform.system() != "Darwin":
        print(
            "On this platform, set DEEPSEEK_API_KEY in the environment that launches Codex.",
            file=sys.stderr,
        )
        return 1
    if not sys.stdin.isatty():
        print("Run this command in an interactive terminal.", file=sys.stderr)
        return 1
    print("Paste the DeepSeek API key at the macOS Keychain prompt.")
    print("The value is not echoed and is not written to shell history or Codex config.")
    return subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-a",
            getpass.getuser(),
            "-s",
            SERVICE,
            "-U",
            "-w",
        ],
        check=False,
    ).returncode


def check() -> int:
    problems: list[str] = []
    if shutil.which("codex") is None:
        problems.append("codex executable not found")
    if not profile_path().exists():
        problems.append(f"profile missing: {profile_path()}")
    catalog = skill_dir() / "assets" / "models.json"
    if not catalog.exists():
        problems.append(f"model catalog missing: {catalog}")

    key_result = subprocess.run(
        [sys.executable, str(skill_dir() / "scripts" / "deepseek_key.py")],
        check=False,
        capture_output=True,
        text=True,
    )
    if key_result.returncode != 0:
        problems.append("API key is not available")

    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print(
            "Store the key with: python3 "
            "~/.codex/skills/delegate-to-deepseek/scripts/setup.py store-key",
            file=sys.stderr,
        )
        return 1

    print("DeepSeek profile and credential are ready.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("install", "store-key", "check"))
    return parser.parse_args()


def main() -> int:
    action = parse_args().action
    if action == "install":
        return install()
    if action == "store-key":
        return store_key()
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
