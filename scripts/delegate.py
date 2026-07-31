#!/usr/bin/env python3
"""Run DeepSeek V4 Flash as an ephemeral Codex or Claude Code subagent."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import platform
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable


CHILD_GUARD = """

Subagent constraints:
- Work only on the task above and inspect the repository for evidence.
- Do not invoke delegate-to-deepseek, spawn subagents, or start another agent process.
- Report concrete file paths and commands used.
""".strip()

# DeepSeek's Anthropic-compatible surface. It speaks the Messages API directly,
# so the Claude Code backend needs no local TLS bridge and no model catalog.
ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"

# Tools the child may use. `Task` is never granted, so the child cannot fan out
# into further subagents regardless of what the task text asks for.
REVIEW_TOOLS = ("Read", "Grep", "Glob")
WRITE_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DeepSeek V4 Flash inside a bounded Codex or Claude Code child process."
    )
    parser.add_argument("--task", help="Task text; read stdin when omitted.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Child working directory.")
    parser.add_argument(
        "--backend",
        choices=("codex", "claude"),
        default="codex",
        help="Agent harness that hosts DeepSeek (default: codex).",
    )
    parser.add_argument("--mode", choices=("review", "write"), default="review")
    parser.add_argument("--reasoning", choices=("high", "max"), default="high")
    parser.add_argument(
        "--model",
        default="deepseek-v4-flash",
        help="DeepSeek model slug; claude backend also accepts deepseek-v4-pro.",
    )
    parser.add_argument("--profile", default="deepseek-flash", help="Codex profile name.")
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds before termination.")
    parser.add_argument("--structured", action="store_true")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument(
        "--shell",
        action="store_true",
        help="Claude backend: also grant Bash in review mode (see README caveat).",
    )
    parser.add_argument(
        "--transport",
        choices=("auto", "native", "curl"),
        default="auto",
        help="Codex backend: use a loopback curl TLS bridge automatically on macOS.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


@dataclass
class Plan:
    """A ready-to-launch child process plus how to read its final answer."""

    command: list[str]
    env: dict[str, str] | None
    extract_final: Callable[[list[str]], str | None]
    observe_line: Callable[[str], None] | None = None


def command_string(parts: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(parts)
    return shlex.join(parts)


def popen_kwargs() -> dict[str, object]:
    """Child-process creation flags that work on the current platform.

    POSIX puts each child in its own session/process group so the whole tree
    can be signalled; Windows uses a new process group so `taskkill /T` can
    tear the tree down without POSIX-only APIs.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate ``process`` and its descendants without POSIX-only APIs."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            capture_output=True,
        )
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class OutputPump:
    """Drain a child's stdout on a daemon thread into a bounded queue.

    ``select.select`` only supports sockets on Windows, so this thread-based
    pump is the cross-platform replacement for the POSIX-only pipe select.
    """

    def __init__(self, stream: object):
        self._stream = stream
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(
            target=self._run,
            name="delegate-output-pump",
            daemon=True,
        )

    def _run(self) -> None:
        try:
            for line in self._stream:  # type: ignore[union-attr]
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def start(self) -> None:
        self._thread.start()

    def next_line(self, timeout: float) -> str | None:
        """Return the next line, ``None`` on timeout, or raise ``EOFError``."""
        try:
            item = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            raise EOFError("output stream closed")
        return item


def start_curl_bridge(
    scripts_dir: Path,
    *,
    stderr: object | None = None,
) -> tuple[subprocess.Popen[str], str]:
    bridge = scripts_dir / "curl_bridge.py"
    key_helper = scripts_dir / "deepseek_key.py"
    process = subprocess.Popen(
        [sys.executable, str(bridge), "--key-command", str(key_helper)],
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
        bufsize=1,
        **popen_kwargs(),
    )
    assert process.stdout is not None
    pump = OutputPump(process.stdout)
    pump.start()
    try:
        line = pump.next_line(10.0)
    except EOFError:
        line = None
    if line is None:
        terminate_process_tree(process)
        raise RuntimeError("the local curl bridge did not start")
    base_url = line.strip()
    if process.poll() is not None or not base_url.startswith("http://127.0.0.1:"):
        terminate_process_tree(process)
        raise RuntimeError("the local curl bridge returned an invalid address")
    return process, base_url


def stream_child_output(
    process: subprocess.Popen[str],
    deadline: float,
    collected: list[str],
) -> int | None:
    """Stream child stdout to stderr until EOF or the deadline.

    Every line is appended to ``collected`` so a backend can recover the final
    answer from the event stream. Returns ``None`` on EOF and 124 when the
    deadline expires first.
    """
    assert process.stdout is not None
    pump = OutputPump(process.stdout)
    pump.start()
    while True:
        try:
            line = pump.next_line(1.0)
        except EOFError:
            return None
        if line is None:
            if process.poll() is not None:
                return None
            if time.monotonic() >= deadline:
                return 124
            continue
        collected.append(line)
        print(line, end="", file=sys.stderr, flush=True)


def build_codex_plan(
    args: argparse.Namespace,
    cwd: Path,
    scripts_dir: Path,
    schema: Path,
    final_path: Path,
    bridge_url: str | None,
) -> Plan:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex executable not found")

    sandbox = "read-only" if args.mode == "review" else "workspace-write"
    command = [
        codex,
        "exec",
        "--profile",
        args.profile,
        "--ephemeral",
        "--json",
        "--disable",
        "multi_agent",
        "--disable",
        "multi_agent_v2",
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
        "-c",
        'approval_policy="never"',
        "-c",
        f'model_reasoning_effort="{args.reasoning}"',
        "-C",
        str(cwd),
        "--output-last-message",
        str(final_path),
    ]
    if bridge_url:
        command.extend(("-c", f'model_providers.deepseek.base_url="{bridge_url}"'))
    for extra in args.add_dir:
        command.extend(("--add-dir", str(Path(extra).expanduser().resolve())))
    if args.structured:
        command.extend(("--output-schema", str(schema)))
    command.append("-")

    def extract_final(_lines: list[str]) -> str | None:
        if not final_path.exists():
            return None
        return final_path.read_text(encoding="utf-8").strip()

    return Plan(command=command, env=None, extract_final=extract_final)


def claude_child_env() -> dict[str, str]:
    """Environment that pins the child to DeepSeek and to nothing else.

    Every inherited ``ANTHROPIC_*`` and ``CLAUDE_CODE_*`` variable is dropped so
    a parent Claude Code session cannot leak its own endpoint, model, or
    credential into the child. Combined with ``--bare`` -- which never reads
    OAuth or the keychain -- the only usable credential is the DeepSeek key that
    ``apiKeyHelper`` returns, so the child cannot silently bill an Anthropic
    subscription. ``DEEPSEEK_API_KEY`` survives because the key helper reads it.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
    }
    env["ANTHROPIC_BASE_URL"] = ANTHROPIC_BASE_URL
    return env


def claude_json_schema(schema: Path) -> str:
    """Return the shared result schema in the dialect ``--json-schema`` accepts.

    Codex resolves the draft 2020-12 ``$schema`` meta-reference; Claude Code
    rejects it outright, so drop the key rather than fork the schema file.
    """
    document = json.loads(schema.read_text(encoding="utf-8"))
    document.pop("$schema", None)
    return json.dumps(document)


def build_claude_plan(
    args: argparse.Namespace,
    scripts_dir: Path,
    schema: Path,
) -> Plan:
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude executable not found")

    # `--tools` decides which tools exist; `--allowedTools` decides which run
    # without approval. Write mode must grant both, or the child stalls retrying
    # commands the permission gate keeps denying.
    if args.mode == "write":
        tools = list(WRITE_TOOLS)
        allowed = ["Bash", "Edit", "Write"]
        permission_mode = "acceptEdits"
    else:
        tools = list(REVIEW_TOOLS)
        allowed = []
        if args.shell:
            # Deliberately left out of `--allowedTools`: the built-in classifier
            # then auto-runs read-only commands (ls, cat, grep, git status) and
            # denies anything that could mutate the workspace.
            tools.append("Bash")
        permission_mode = "dontAsk"

    key_helper = command_string([sys.executable, str(scripts_dir / "deepseek_key.py")])
    settings = json.dumps({"apiKeyHelper": key_helper})

    command = [
        claude,
        "--bare",
        "--print",
        "--model",
        args.model,
        "--effort",
        args.reasoning,
        "--settings",
        settings,
        "--tools",
        ",".join(tools),
        "--permission-mode",
        permission_mode,
        "--no-session-persistence",
        *(("--allowedTools", ",".join(allowed)) if allowed else ()),
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    for extra in args.add_dir:
        command.extend(("--add-dir", str(Path(extra).expanduser().resolve())))
    if args.structured:
        command.extend(("--json-schema", claude_json_schema(schema)))

    def extract_final(lines: list[str]) -> str | None:
        """Pull `.result` out of the last `type: result` event on the stream."""
        for line in reversed(lines):
            line = line.strip()
            if not line.startswith("{") or '"result"' not in line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "result":
                continue
            if event.get("is_error"):
                subtype = event.get("subtype", "error")
                print(f"Claude backend reported {subtype}.", file=sys.stderr)
                return None
            result = event.get("result")
            return result.strip() if isinstance(result, str) else None
        return None

    return Plan(command=command, env=claude_child_env(), extract_final=extract_final)


def main() -> int:
    args = parse_args()
    scripts_dir = Path(__file__).resolve().parent
    schema = scripts_dir.parent / "assets" / "result.schema.json"

    if args.backend == "claude":
        if args.transport == "curl":
            print(
                "--transport curl only applies to --backend codex; DeepSeek's "
                "Anthropic endpoint is reached directly.",
                file=sys.stderr,
            )
            return 2
    elif args.transport == "curl" and platform.system() != "Darwin":
        print("--transport curl is only supported on macOS.", file=sys.stderr)
        return 2

    if args.shell and args.backend != "claude":
        print("--shell only applies to --backend claude.", file=sys.stderr)
        return 2

    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        print(f"working directory does not exist: {cwd}", file=sys.stderr)
        return 2

    task = args.task if args.task is not None else sys.stdin.read()
    task = task.strip()
    if not task:
        print("task is empty", file=sys.stderr)
        return 2
    task = f"{task}\n\n{CHILD_GUARD}\n"

    if not args.dry_run:
        key_helper = scripts_dir / "deepseek_key.py"
        key_check = subprocess.run(
            [sys.executable, str(key_helper)],
            check=False,
            capture_output=True,
            text=True,
        )
        if key_check.returncode != 0 or not key_check.stdout.strip():
            print("Opening the DeepSeek API key window...", file=sys.stderr)
            setup_result = subprocess.run(
                [sys.executable, str(scripts_dir / "setup.py"), "configure"],
                check=False,
            )
            if setup_result.returncode != 0:
                return 3
            key_check = subprocess.run(
                [sys.executable, str(key_helper)],
                check=False,
                capture_output=True,
                text=True,
            )
            if key_check.returncode != 0 or not key_check.stdout.strip():
                print("DeepSeek API key is still unavailable.", file=sys.stderr)
                return 3

    use_curl_bridge = args.backend == "codex" and (
        args.transport == "curl"
        or (
            args.transport == "auto"
            and platform.system() == "Darwin"
            and shutil.which("curl") is not None
        )
    )

    with tempfile.TemporaryDirectory(prefix="deepseek-subagent-") as temp_dir:
        final_path = Path(temp_dir) / "final.txt"
        bridge_process: subprocess.Popen[str] | None = None
        bridge_url: str | None = None
        if use_curl_bridge:
            if args.dry_run:
                bridge_url = "http://127.0.0.1:<port>"
            else:
                try:
                    bridge_process, bridge_url = start_curl_bridge(scripts_dir)
                except RuntimeError as error:
                    print(
                        f"Could not start DeepSeek curl bridge: {error}",
                        file=sys.stderr,
                    )
                    return 2

        try:
            try:
                if args.backend == "claude":
                    plan = build_claude_plan(args, scripts_dir, schema)
                else:
                    plan = build_codex_plan(
                        args, cwd, scripts_dir, schema, final_path, bridge_url
                    )
            except RuntimeError as error:
                print(str(error), file=sys.stderr)
                return 2

            if args.dry_run:
                print(command_string(plan.command))
                return 0

            process = subprocess.Popen(
                plan.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=str(cwd),
                env=plan.env,
                **popen_kwargs(),
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(task)
            process.stdin.close()

            collected: list[str] = []
            deadline = time.monotonic() + args.timeout
            try:
                timeout_status = stream_child_output(process, deadline, collected)
            except KeyboardInterrupt:
                terminate_process_tree(process)
                return 130
            if timeout_status == 124:
                print(
                    f"DeepSeek subagent timed out after {args.timeout} seconds.",
                    file=sys.stderr,
                )
                terminate_process_tree(process)
                return 124

            process.wait()

            if process.returncode != 0:
                print(
                    f"DeepSeek subagent exited with status {process.returncode}.",
                    file=sys.stderr,
                )
                return process.returncode or 1

            final = plan.extract_final(collected)
            if not final:
                print("DeepSeek subagent produced no final message.", file=sys.stderr)
                return 1

            print(final)
            return 0
        finally:
            if bridge_process is not None:
                terminate_process_tree(bridge_process)


if __name__ == "__main__":
    raise SystemExit(main())
