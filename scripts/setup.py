#!/usr/bin/env python3
"""Install the DeepSeek Codex profile and collect its key in a macOS dialog."""

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
KEY_DIALOG_SCRIPT = r'''
set dialogResult to display dialog "Paste your DeepSeek API key. It will be stored in macOS Keychain and will not be written to Codex config." default answer "" with title "Configure DeepSeek for Codex" buttons {"Cancel", "Save"} default button "Save" cancel button "Cancel" with hidden answer
return text returned of dialogResult
'''.strip()


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


def apple_script_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def show_message(message: str, *, critical: bool = False) -> None:
    if platform.system() != "Darwin":
        return
    icon = "critical" if critical else "note"
    script = (
        f'display dialog {apple_script_string(message)} with title "DeepSeek for Codex" '
        f'buttons {{"OK"}} default button "OK" with icon {icon}'
    )
    subprocess.run(
        ["/usr/bin/osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
    )


def prompt_for_key() -> tuple[str | None, int]:
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", KEY_DIALOG_SCRIPT],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, 130
    key = result.stdout.strip()
    if not key:
        show_message("The API key cannot be empty.", critical=True)
        return None, 1
    if not key.startswith("sk-"):
        show_message("The DeepSeek API key must start with sk-.", critical=True)
        return None, 1
    return key, 0


def save_key_to_keychain(
    key: str,
    *,
    service: str = SERVICE,
    account: str | None = None,
) -> int:
    owner = account or getpass.getuser()
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-a",
            owner,
            "-s",
            service,
            "-U",
            "-w",
        ],
        input=f"{key}\n{key}\n",
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode


def store_key() -> int:
    if platform.system() != "Darwin":
        print(
            "On this platform, set DEEPSEEK_API_KEY in the environment that launches Codex.",
            file=sys.stderr,
        )
        return 1
    while True:
        key, status = prompt_for_key()
        if key is not None:
            break
        if status == 130:
            print("DeepSeek configuration cancelled.", file=sys.stderr)
            return status
    try:
        status = save_key_to_keychain(key)
    finally:
        key = ""
    if status != 0:
        show_message("The API key could not be saved to macOS Keychain.", critical=True)
        print("Failed to save the DeepSeek API key to Keychain.", file=sys.stderr)
        return status
    print("DeepSeek API key saved in macOS Keychain.")
    return 0


def configure() -> int:
    status = install()
    if status != 0:
        show_message("The Codex profile could not be installed.", critical=True)
        return status
    status = store_key()
    if status != 0:
        return status
    show_message("DeepSeek V4 Flash is configured and ready for Codex.")
    return 0


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
            "Open the key window with: python3 "
            "~/.codex/skills/delegate-to-deepseek/scripts/setup.py",
            file=sys.stderr,
        )
        return 1

    print("DeepSeek profile and credential are ready.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        nargs="?",
        default="configure",
        choices=("configure", "install", "store-key", "check"),
    )
    return parser.parse_args()


def main() -> int:
    action = parse_args().action
    if action == "configure":
        return configure()
    if action == "install":
        return install()
    if action == "store-key":
        return store_key()
    return check()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("DeepSeek configuration cancelled.", file=sys.stderr)
        raise SystemExit(130)
