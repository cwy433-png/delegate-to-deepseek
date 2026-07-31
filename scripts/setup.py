#!/usr/bin/env python3
"""Install the DeepSeek Codex profile and collect its key in a native dialog.

macOS uses an AppleScript dialog and saves to Keychain; Windows 10/11 uses a
native masked PowerShell/WinForms dialog and saves to Windows Credential
Manager; other platforms rely on the ``DEEPSEEK_API_KEY`` environment variable.
The key never appears in process arguments, shell history, Codex config, Git,
or logs.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

import win_cred  # local sibling module; inert outside Windows


PROFILE_NAME = "deepseek-flash"
SERVICE = "codex-deepseek-api"
MARKER = "# Managed by delegate-to-deepseek"
SKILL_NAME = "delegate-to-deepseek"
# Both installed Claude Code files carry the skill name already -- SKILL.md in
# its frontmatter, the wrapper in its header comment -- so an unrelated file
# that happens to sit at the target path is never clobbered.
MARKER_CLAUDE = SKILL_NAME
# The custom model id defined in .codebuddy/models.json. WorkBuddy reaches
# api.deepseek.com directly through it, so the build in use is knowable.
WORKBUDDY_MODEL_ID = "deepseek-v4-flash-direct"
# (filename, needs the executable bit). Both wrappers install on every platform:
# the inactive one is inert, and shipping both keeps an installed copy complete
# when a home directory is shared or synced between machines.
CLAUDE_SKILL_FILES = (
    ("SKILL.md", False),
    ("deepseek", True),
    ("deepseek.cmd", False),
)
KEY_DIALOG_SCRIPT = r'''
set dialogResult to display dialog "Paste your DeepSeek API key. It will be stored in macOS Keychain and will not be written to Codex config." default answer "" with title "Configure DeepSeek for Codex" buttons {"Cancel", "Save"} default button "Save" cancel button "Cancel" with hidden answer
return text returned of dialogResult
'''.strip()
# Kept in a temp file (never on the command line); the dialog only echoes the
# typed key to stdout, so no secret is written to disk or to process arguments.
WINDOWS_KEY_DIALOG_SCRIPT = r'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$form = New-Object System.Windows.Forms.Form
$form.Text = 'Configure DeepSeek for Codex'
$form.StartPosition = 'CenterScreen'
$form.ClientSize = New-Object System.Drawing.Size(560, 150)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.MinimizeBox = $false

$label = New-Object System.Windows.Forms.Label
$label.Text = 'Paste your DeepSeek API key. It will be stored in Windows Credential Manager and will not be written to Codex config.'
$label.AutoSize = $false
$label.Size = New-Object System.Drawing.Size(520, 44)
$label.Location = New-Object System.Drawing.Point(20, 12)

$textbox = New-Object System.Windows.Forms.TextBox
$textbox.UseSystemPasswordChar = $true
$textbox.Size = New-Object System.Drawing.Size(520, 24)
$textbox.Location = New-Object System.Drawing.Point(20, 62)

$save = New-Object System.Windows.Forms.Button
$save.Text = 'Save'
$save.DialogResult = [System.Windows.Forms.DialogResult]::OK
$save.Location = New-Object System.Drawing.Point(380, 100)

$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = 'Cancel'
$cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
$cancel.Location = New-Object System.Drawing.Point(462, 100)

$form.AcceptButton = $save
$form.CancelButton = $cancel
$form.Controls.Add($label)
$form.Controls.Add($textbox)
$form.Controls.Add($save)
$form.Controls.Add($cancel)

if ($form.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
    [Console]::Out.Write($textbox.Text)
    exit 0
}
exit 130
'''.strip()


def skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def profile_path() -> Path:
    return codex_home() / f"{PROFILE_NAME}.config.toml"


def toml_basic_string(value: str) -> str:
    """Quote a path as a TOML basic string, escaping backslashes and quotes.

    Windows paths contain backslashes (and may contain quotes or spaces), so a
    raw interpolation would produce invalid TOML; this keeps the generated
    profile parseable on every platform.
    """
    escaped: list[str] = []
    for char in value:
        if char == "\\":
            escaped.append("\\\\")
        elif char == '"':
            escaped.append('\\"')
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\r":
            escaped.append("\\r")
        elif char == "\t":
            escaped.append("\\t")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            escaped.append(f"\\u{ord(char):04X}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def _profile_text(
    catalog: Path,
    key_command: Path,
    python_command: Path | None = None,
) -> str:
    python_command = python_command or Path(sys.executable)
    return f"""{MARKER}
model = "deepseek-v4-flash"
model_provider = "deepseek"
model_catalog_json = {toml_basic_string(str(catalog))}
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
command = {toml_basic_string(str(python_command))}
args = [{toml_basic_string(str(key_command))}]
timeout_ms = 5000
refresh_interval_ms = 0
"""


def profile_text() -> str:
    root = skill_dir()
    return _profile_text(
        root / "assets" / "models.json",
        root / "scripts" / "deepseek_key.py",
    )


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


def claude_home() -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".claude"


def claude_skill_source() -> Path:
    """The version-controlled Claude Code half of this repository."""
    return skill_dir() / ".claude" / "skills" / SKILL_NAME


def claude_skill_target() -> Path:
    return claude_home() / "skills" / SKILL_NAME


def write_file_atomically(
    target: Path,
    data: str,
    *,
    executable: bool = False,
    mode: int | None = None,
) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as handle:
        handle.write(data)
        temporary = Path(handle.name)
    os.replace(temporary, target)
    target.chmod(mode if mode is not None else (0o755 if executable else 0o644))


def install_claude() -> int:
    """Copy the Claude Code skill into the user's skills directory.

    Claude Code only reads its own skills directory, so the repository cannot
    serve it in place the way it serves Codex. Copying keeps one source of truth
    in Git; rerun this action after the repository changes. Files that this
    action did not write are never overwritten.
    """
    source = claude_skill_source()
    if not source.is_dir():
        print(f"Claude skill source missing: {source}", file=sys.stderr)
        return 1

    target = claude_skill_target()
    target.mkdir(parents=True, exist_ok=True)

    copied = 0
    for name, executable in CLAUDE_SKILL_FILES:
        origin = source / name
        if not origin.is_file():
            print(f"Claude skill file missing: {origin}", file=sys.stderr)
            return 1
        desired = origin.read_text(encoding="utf-8")
        destination = target / name
        if destination.exists():
            current = destination.read_text(encoding="utf-8")
            if current == desired:
                continue
            if MARKER_CLAUDE not in current:
                print(
                    f"Refusing to overwrite unmanaged file: {destination}",
                    file=sys.stderr,
                )
                return 1
        write_file_atomically(destination, desired, executable=executable)
        copied += 1

    if copied == 0:
        print(f"Claude Code skill already current: {target}")
    else:
        print(f"Installed Claude Code skill: {target}")
    return 0


def claude_skill_is_current() -> bool:
    source = claude_skill_source()
    target = claude_skill_target()
    for name, _ in CLAUDE_SKILL_FILES:
        origin = source / name
        destination = target / name
        if not origin.is_file() or not destination.is_file():
            return False
        if origin.read_text(encoding="utf-8") != destination.read_text(encoding="utf-8"):
            return False
    return True


def workbuddy_home() -> Path:
    configured = (
        os.environ.get("WORKBUDDY_CONFIG_DIR") or os.environ.get("CODEBUDDY_CONFIG_DIR")
    )
    return Path(configured).expanduser() if configured else Path.home() / ".workbuddy"


def workbuddy_settings_path() -> Path:
    return workbuddy_home() / "settings.json"


def stored_key() -> str | None:
    """Return the stored DeepSeek key, or ``None``.

    Delegates to ``deepseek_key.py`` so the per-platform credential lookup lives
    in exactly one place.
    """
    result = subprocess.run(
        [sys.executable, str(skill_dir() / "scripts" / "deepseek_key.py")],
        check=False,
        capture_output=True,
        text=True,
    )
    key = result.stdout.strip()
    return key if result.returncode == 0 and key else None


def workbuddy_env_updates(key: str) -> dict[str, str]:
    """The two variables WorkBuddy needs to reach DeepSeek directly.

    A custom model resolves ``apiKey`` only by expanding ``${VAR}`` from
    ``process.env``, so the key has to arrive as an environment variable;
    ``CODEBUDDY_SMALL_FAST_MODEL`` then points the ``lite`` variant -- the one
    ``Explore`` sub-agents declare -- at that model.
    """
    return {
        "DEEPSEEK_API_KEY": key,
        "CODEBUDDY_SMALL_FAST_MODEL": WORKBUDDY_MODEL_ID,
    }


def workbuddy_env_is_current() -> bool:
    """Whether WorkBuddy's settings already point the lite variant at DeepSeek.

    Checks that a key is present without reading its value anywhere it could be
    printed.
    """
    target = workbuddy_settings_path()
    if not target.is_file():
        return False
    try:
        settings = json.loads(target.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return False
    env = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(env, dict):
        return False
    return bool(env.get("DEEPSEEK_API_KEY")) and (
        env.get("CODEBUDDY_SMALL_FAST_MODEL") == WORKBUDDY_MODEL_ID
    )


def install_workbuddy() -> int:
    """Merge the DeepSeek variables into WorkBuddy's settings.

    WorkBuddy's ``settings.json`` holds unrelated user state (enabled plugins,
    channel configuration), so this reads, merges, and writes back rather than
    replacing the file, and refuses to touch it at all if it does not parse.
    """
    models = skill_dir() / ".codebuddy" / "models.json"
    if not models.is_file():
        print(f"Custom model definition missing: {models}", file=sys.stderr)
        return 1

    key = stored_key()
    if key is None:
        print("Opening the DeepSeek API key window...", file=sys.stderr)
        status = store_key()
        if status != 0:
            return status
        key = stored_key()
        if key is None:
            print("DeepSeek API key is still unavailable.", file=sys.stderr)
            return 3

    target = workbuddy_settings_path()
    if not target.parent.is_dir():
        print(
            f"WorkBuddy config directory not found: {target.parent}\n"
            "Install and launch WorkBuddy once before running this action.",
            file=sys.stderr,
        )
        return 1

    settings: dict[str, object] = {}
    if target.exists():
        try:
            parsed = json.loads(target.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError as error:
            print(
                f"Refusing to rewrite unparsable settings: {target} ({error})",
                file=sys.stderr,
            )
            return 1
        if not isinstance(parsed, dict):
            print(f"Refusing to rewrite non-object settings: {target}", file=sys.stderr)
            return 1
        settings = parsed

    current_env = settings.get("env")
    if current_env is not None and not isinstance(current_env, dict):
        print(f'Refusing to overwrite a non-object "env" in {target}', file=sys.stderr)
        return 1

    env = dict(current_env or {})
    updates = workbuddy_env_updates(key)
    key = ""
    if all(env.get(name) == value for name, value in updates.items()):
        print(f"WorkBuddy settings already current: {target}")
        return 0

    env.update(updates)
    settings["env"] = env
    write_file_atomically(
        target,
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )
    print(f"Updated WorkBuddy settings: {target}")
    print("Restart WorkBuddy so it picks up the new environment.")
    return 0


def apple_script_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def show_message(message: str, *, critical: bool = False) -> None:
    system = platform.system()
    if system == "Darwin":
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
    elif system == "Windows":
        import ctypes

        flags = 0x10 if critical else 0x40
        ctypes.windll.user32.MessageBoxW(None, message, "DeepSeek for Codex", flags)


def validate_key(key: str) -> str | None:
    if not key:
        return "The API key cannot be empty."
    if not key.startswith("sk-"):
        return "The DeepSeek API key must start with sk-."
    return None


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
    error = validate_key(key)
    if error:
        show_message(error, critical=True)
        return None, 1
    return key, 0


def prompt_for_key_windows() -> tuple[str | None, int]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        print(
            "powershell.exe was not found; cannot open the native key dialog.",
            file=sys.stderr,
        )
        return None, 1
    with tempfile.TemporaryDirectory(prefix="deepseek-key-dialog-") as tmp:
        script = Path(tmp) / "key_dialog.ps1"
        script.write_text(WINDOWS_KEY_DIALOG_SCRIPT, encoding="utf-8")
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-STA",
                "-File",
                str(script),
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    if result.returncode == 130:
        return None, 130
    if result.returncode != 0:
        detail = result.stderr.strip()
        if detail:
            print(f"The key dialog failed: {detail}", file=sys.stderr)
        return None, 1
    key = result.stdout.strip()
    error = validate_key(key)
    if error:
        show_message(error, critical=True)
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
        # `security` reads from the controlling terminal when one exists, even
        # when stdin is piped. Detach the child session so `-w` consumes the
        # password from this private pipe instead of opening a second prompt.
        start_new_session=True,
    )
    return result.returncode


def save_key_windows(key: str) -> int:
    try:
        win_cred.write_credential(SERVICE, key, user=getpass.getuser())
    except OSError as error:
        show_message(
            "The API key could not be saved to Windows Credential Manager.",
            critical=True,
        )
        print(
            f"Failed to save the DeepSeek API key to Windows Credential Manager: {error}",
            file=sys.stderr,
        )
        return 1
    return 0


def store_key_macos() -> int:
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


def store_key_windows() -> int:
    while True:
        key, status = prompt_for_key_windows()
        if key is not None:
            break
        if status == 130:
            print("DeepSeek configuration cancelled.", file=sys.stderr)
            return status
    try:
        status = save_key_windows(key)
    finally:
        key = ""
    if status != 0:
        return status
    print("DeepSeek API key saved in Windows Credential Manager.")
    return 0


def store_key() -> int:
    system = platform.system()
    if system == "Darwin":
        return store_key_macos()
    if system == "Windows":
        return store_key_windows()
    print(
        "On this platform, set DEEPSEEK_API_KEY in the environment that launches Codex.",
        file=sys.stderr,
    )
    return 1


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

    # The Claude Code half is optional, so report drift as advice rather than
    # as a failure: `codex` alone is a complete install.
    if shutil.which("claude") is not None and not claude_skill_is_current():
        print(
            f"NOTE: Claude Code skill is missing or stale at {claude_skill_target()}. "
            f"Refresh it with: {sys.executable} "
            f"{skill_dir() / 'scripts' / 'setup.py'} install-claude",
            file=sys.stderr,
        )

    # WorkBuddy is optional too, and only worth mentioning once it is installed.
    if workbuddy_home().is_dir() and not workbuddy_env_is_current():
        print(
            f"NOTE: WorkBuddy is not pointed at DeepSeek yet. Configure it with: "
            f"{sys.executable} {skill_dir() / 'scripts' / 'setup.py'} install-workbuddy",
            file=sys.stderr,
        )

    if problems:
        for problem in problems:
            print(f"ERROR: {problem}", file=sys.stderr)
        print(
            f"Open the key window with: {sys.executable} "
            f"{skill_dir() / 'scripts' / 'setup.py'}",
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
        choices=(
            "configure",
            "install",
            "install-claude",
            "install-workbuddy",
            "store-key",
            "check",
        ),
    )
    return parser.parse_args()


def main() -> int:
    action = parse_args().action
    if action == "configure":
        return configure()
    if action == "install":
        return install()
    if action == "install-claude":
        return install_claude()
    if action == "install-workbuddy":
        return install_workbuddy()
    if action == "store-key":
        return store_key()
    return check()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("DeepSeek configuration cancelled.", file=sys.stderr)
        raise SystemExit(130)
