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
# WorkBuddy sends a custom model's id verbatim as the API `model` field, so this
# must be the real DeepSeek slug rather than a repository-local alias.
WORKBUDDY_MODEL_ID = "deepseek-v4-flash"
WORKBUDDY_AGENT_MODEL_ID = f"custom-local:{WORKBUDDY_MODEL_ID}"
LEGACY_WORKBUDDY_MODEL_ID = "deepseek-v4-flash-direct"
MANAGED_WORKBUDDY_LITE_MODEL_IDS = (
    LEGACY_WORKBUDDY_MODEL_ID,
    WORKBUDDY_MODEL_ID,
    WORKBUDDY_AGENT_MODEL_ID,
)
WORKBUDDY_MACOS_NODE_OPTION = "--dns-result-order=ipv6first"
WORKBUDDY_AGENT_NAME = "deepseek"
MARKER_WORKBUDDY_AGENT = "Managed by delegate-to-deepseek"
# (filename, needs the executable bit). Both wrappers install on every platform:
# the inactive one is inert, and shipping both keeps an installed copy complete
# when a home directory is shared or synced between machines.
CLAUDE_SKILL_FILES = (
    ("SKILL.md", False),
    ("deepseek", True),
    ("deepseek.cmd", False),
)
# What Codex needs at run time, and nothing else. `setup.py`, the tests, the
# other harnesses' directories, and the prose files stay in the repository:
# they are how the skill is developed, not how it runs.
CODEX_SKILL_FILES = (
    "SKILL.md",
    "agents/openai.yaml",
    "assets/models.json",
    "assets/result.schema.json",
    "scripts/curl_bridge.py",
    "scripts/deepseek_key.py",
    "scripts/delegate.py",
    "scripts/win_cred.py",
)
# The installed tree mixes Markdown, YAML, JSON, and Python, so a per-file
# marker comment is not available in every syntax. Stamp the directory instead
# and refuse to write into one that lacks it, which keeps the installer from
# ever clobbering a directory it did not create.
CODEX_SKILL_STAMP = ".managed-by-delegate-to-deepseek"
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


def codex_skill_target() -> Path:
    """Where Codex loads the skill from.

    Codex only reads its own skills directory, so the repository is installed
    here rather than living here. Keeping the repository out of this path is
    what lets it sit in an ordinary project directory.
    """
    return codex_home() / "skills" / SKILL_NAME


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
    # Point at the installed copy, never at this repository. A profile that
    # referenced the working tree would break the moment the repository moved
    # or was deleted, which is exactly what installing is meant to prevent.
    root = codex_skill_target()
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


def install_codex() -> int:
    """Copy the Codex half of this repository into Codex's skills directory.

    Mirrors ``install_claude``: the repository stays the one source of truth in
    Git and every harness gets an installed copy. Rerun after the repository
    changes; ``check`` reports drift.
    """
    source = skill_dir()
    target = codex_skill_target()
    if target.resolve() == source.resolve():
        print(
            f"Refusing to install onto the repository itself: {target}",
            file=sys.stderr,
        )
        return 1

    stamp = target / CODEX_SKILL_STAMP
    if target.exists() and not stamp.is_file():
        print(f"Refusing to overwrite unmanaged directory: {target}", file=sys.stderr)
        return 1

    copied = 0
    for name in CODEX_SKILL_FILES:
        origin = source / name
        if not origin.is_file():
            print(f"Codex skill file missing: {origin}", file=sys.stderr)
            return 1
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        desired = origin.read_text(encoding="utf-8")
        if destination.is_file() and destination.read_text(encoding="utf-8") == desired:
            continue
        write_file_atomically(destination, desired)
        copied += 1

    if not stamp.is_file():
        write_file_atomically(stamp, f"{MARKER}\n")

    if copied == 0:
        print(f"Codex skill already current: {target}")
    else:
        print(f"Installed Codex skill: {target}")
    return 0


def codex_skill_is_current() -> bool:
    source = skill_dir()
    target = codex_skill_target()
    if not (target / CODEX_SKILL_STAMP).is_file():
        return False
    for name in CODEX_SKILL_FILES:
        origin = source / name
        destination = target / name
        if not origin.is_file() or not destination.is_file():
            return False
        if origin.read_text(encoding="utf-8") != destination.read_text(encoding="utf-8"):
            return False
    return True


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


def codebuddy_home() -> Path:
    """Return the user-level directory for portable models and agents.

    WorkBuddy keeps desktop settings under ``~/.workbuddy`` but discovers
    user-level custom models and agents under ``~/.codebuddy``.
    """
    configured = os.environ.get("CODEBUDDY_CONFIG_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".codebuddy"


def workbuddy_model_source() -> Path:
    return skill_dir() / ".codebuddy" / "models.json"


def workbuddy_models_target() -> Path:
    return codebuddy_home() / "models.json"


def workbuddy_agent_source() -> Path:
    return skill_dir() / ".codebuddy" / "agents" / f"{WORKBUDDY_AGENT_NAME}.md"


def workbuddy_agent_target() -> Path:
    return codebuddy_home() / "agents" / f"{WORKBUDDY_AGENT_NAME}.md"


def workbuddy_model_definition() -> dict[str, object]:
    source = workbuddy_model_source()
    document = json.loads(source.read_text(encoding="utf-8"))
    models = document.get("models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        raise ValueError(f'"models" must be an array in {source}')
    for model in models:
        if isinstance(model, dict) and model.get("id") == WORKBUDDY_MODEL_ID:
            return model
    raise ValueError(f"model {WORKBUDDY_MODEL_ID!r} is missing from {source}")


def workbuddy_model_is_compatible(model: object) -> bool:
    """Whether an existing entry is safe for the installed agent to use."""
    if not isinstance(model, dict):
        return False
    return (
        model.get("id") == WORKBUDDY_MODEL_ID
        and model.get("url")
        == "https://api.deepseek.com/v1/chat/completions"
        and model.get("apiKey") == "${DEEPSEEK_API_KEY}"
        and model.get("supportsToolCall") is True
    )


def workbuddy_model_is_current() -> bool:
    target = workbuddy_models_target()
    if not target.is_file():
        return False
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(document, dict):
        return False
    models = document.get("models")
    available = document.get("availableModels")
    return (
        isinstance(models, list)
        and any(workbuddy_model_is_compatible(model) for model in models)
        and isinstance(available, list)
        and WORKBUDDY_MODEL_ID in available
    )


def install_workbuddy_model() -> int:
    """Merge the repository's model entry into the user-level model catalog."""
    source = workbuddy_model_source()
    if not source.is_file():
        print(f"Custom model definition missing: {source}", file=sys.stderr)
        return 1
    try:
        desired = workbuddy_model_definition()
    except (json.JSONDecodeError, OSError, ValueError) as error:
        print(f"Invalid custom model definition: {error}", file=sys.stderr)
        return 1

    target = workbuddy_models_target()
    document: dict[str, object] = {}
    if target.exists():
        try:
            parsed = json.loads(target.read_text(encoding="utf-8") or "{}")
        except (json.JSONDecodeError, OSError) as error:
            print(
                f"Refusing to rewrite unparsable models file: {target} ({error})",
                file=sys.stderr,
            )
            return 1
        if not isinstance(parsed, dict):
            print(f"Refusing to rewrite non-object models file: {target}", file=sys.stderr)
            return 1
        document = parsed

    current_models = document.get("models")
    if current_models is not None and not isinstance(current_models, list):
        print(f'Refusing to overwrite a non-array "models" in {target}', file=sys.stderr)
        return 1
    models = list(current_models or [])
    matching = [
        model
        for model in models
        if isinstance(model, dict) and model.get("id") == WORKBUDDY_MODEL_ID
    ]
    if matching and not all(workbuddy_model_is_compatible(model) for model in matching):
        print(
            f"Refusing to overwrite a conflicting model named {WORKBUDDY_MODEL_ID!r} "
            f"in {target}",
            file=sys.stderr,
        )
        return 1
    if not matching:
        models.append(desired)

    current_available = document.get("availableModels")
    if current_available is not None and not isinstance(current_available, list):
        print(
            f'Refusing to overwrite a non-array "availableModels" in {target}',
            file=sys.stderr,
        )
        return 1
    available = list(current_available or [])
    if WORKBUDDY_MODEL_ID not in available:
        available.append(WORKBUDDY_MODEL_ID)

    document["models"] = models
    document["availableModels"] = available
    desired_text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if target.is_file() and target.read_text(encoding="utf-8") == desired_text:
        target.chmod(0o600)
        print(f"WorkBuddy global model already current: {target}")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    write_file_atomically(target, desired_text, mode=0o600)
    print(f"Installed WorkBuddy global model: {target}")
    return 0


def workbuddy_agent_is_current() -> bool:
    source = workbuddy_agent_source()
    target = workbuddy_agent_target()
    return (
        source.is_file()
        and target.is_file()
        and source.read_text(encoding="utf-8") == target.read_text(encoding="utf-8")
    )


def install_workbuddy_agent() -> int:
    """Install the native WorkBuddy subagent without clobbering user content."""
    source = workbuddy_agent_source()
    if not source.is_file():
        print(f"WorkBuddy agent definition missing: {source}", file=sys.stderr)
        return 1
    desired = source.read_text(encoding="utf-8")
    if MARKER_WORKBUDDY_AGENT not in desired:
        print(f"WorkBuddy agent marker missing: {source}", file=sys.stderr)
        return 1
    target = workbuddy_agent_target()
    if target.exists():
        current = target.read_text(encoding="utf-8")
        if current == desired:
            print(f"WorkBuddy agent already current: {target}")
            return 0
        if MARKER_WORKBUDDY_AGENT not in current:
            print(
                f"Refusing to overwrite unmanaged WorkBuddy agent: {target}",
                file=sys.stderr,
            )
            return 1
    target.parent.mkdir(parents=True, exist_ok=True)
    write_file_atomically(target, desired)
    print(f"Installed WorkBuddy agent: {target}")
    return 0


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


def workbuddy_env_updates(
    key: str,
    current_env: dict[str, object] | None = None,
) -> dict[str, str]:
    """The environment WorkBuddy needs to reach DeepSeek directly.

    A custom model resolves ``apiKey`` only by expanding ``${VAR}`` from
    ``process.env``, so the key has to arrive as an environment variable.
    WorkBuddy's Agent tool cannot select a model per call; built-in subagents
    such as Explore request the ``lite`` variant instead. Point that variant at
    the namespaced custom-model id so automatic delegation reaches DeepSeek
    rather than WorkBuddy's built-in model with the same bare id.
    """
    updates = {
        "DEEPSEEK_API_KEY": key,
        "CODEBUDDY_SMALL_FAST_MODEL": WORKBUDDY_AGENT_MODEL_ID,
    }
    if platform.system() == "Darwin":
        node_options = str((current_env or {}).get("NODE_OPTIONS") or "").split()
        if WORKBUDDY_MACOS_NODE_OPTION not in node_options:
            node_options.append(WORKBUDDY_MACOS_NODE_OPTION)
        updates["NODE_OPTIONS"] = " ".join(node_options)
    return updates


def workbuddy_node_options_are_current(env: dict[str, object]) -> bool:
    if platform.system() != "Darwin":
        return True
    node_options = env.get("NODE_OPTIONS")
    return isinstance(node_options, str) and (
        WORKBUDDY_MACOS_NODE_OPTION in node_options.split()
    )


def workbuddy_env_is_current() -> bool:
    """Whether WorkBuddy's settings make the DeepSeek key available.

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
    return (
        bool(env.get("DEEPSEEK_API_KEY"))
        and env.get("CODEBUDDY_SMALL_FAST_MODEL") == WORKBUDDY_AGENT_MODEL_ID
        and workbuddy_node_options_are_current(env)
    )


def install_workbuddy() -> int:
    """Install the global model and agent, then expose the key to WorkBuddy.

    WorkBuddy's ``settings.json`` holds unrelated user state (enabled plugins,
    channel configuration), so this reads, merges, and writes back rather than
    replacing the file, and refuses to touch it at all if it does not parse.
    """
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
    if isinstance(current_env, dict):
        node_options = current_env.get("NODE_OPTIONS")
        if node_options is not None and not isinstance(node_options, str):
            print(
                f'Refusing to overwrite a non-string "env.NODE_OPTIONS" in {target}',
                file=sys.stderr,
            )
            return 1

    status = install_workbuddy_model()
    if status != 0:
        return status
    status = install_workbuddy_agent()
    if status != 0:
        return status

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

    env = dict(current_env or {})
    updates = workbuddy_env_updates(key, env)
    key = ""
    if all(env.get(name) == value for name, value in updates.items()):
        target.chmod(0o600)
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
    print("Restart WorkBuddy so it discovers the global model and agent.")
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
    status = install_codex()
    if status != 0:
        show_message("The Codex skill could not be installed.", critical=True)
        return status
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
    # Check the installed copy, not the repository: that is what the profile
    # resolves at run time, so a repository that is fine on disk proves nothing.
    catalog = codex_skill_target() / "assets" / "models.json"
    if not catalog.exists():
        problems.append(f"model catalog missing: {catalog}")
    if not codex_skill_is_current():
        problems.append(
            f"Codex skill is missing or stale at {codex_skill_target()}; "
            f"refresh it with: {sys.executable} "
            f"{skill_dir() / 'scripts' / 'setup.py'} install-codex"
        )

    key_helper = codex_skill_target() / "scripts" / "deepseek_key.py"
    if not key_helper.is_file():
        key_helper = skill_dir() / "scripts" / "deepseek_key.py"
    key_result = subprocess.run(
        [sys.executable, str(key_helper)],
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
    if workbuddy_home().is_dir() and not (
        workbuddy_env_is_current()
        and workbuddy_model_is_current()
        and workbuddy_agent_is_current()
    ):
        print(
            f"NOTE: WorkBuddy's DeepSeek model or agent is missing or stale. "
            f"Configure it with: "
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
            "install-codex",
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
    if action == "install-codex":
        return install_codex()
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
