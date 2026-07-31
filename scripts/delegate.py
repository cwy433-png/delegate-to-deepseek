#!/usr/bin/env python3
"""Run DeepSeek V4 Flash as an ephemeral Codex subagent."""

from __future__ import annotations

import argparse
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


CHILD_GUARD = """

Subagent constraints:
- Work only on the task above and inspect the repository for evidence.
- Do not invoke delegate-to-deepseek, spawn subagents, or start another Codex process.
- Report concrete file paths and commands used.
""".strip()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run DeepSeek V4 Flash inside a bounded Codex child process."
    )
    parser.add_argument("--task", help="Task text; read stdin when omitted.")
    parser.add_argument("--cwd", default=os.getcwd(), help="Child working directory.")
    parser.add_argument("--mode", choices=("review", "write"), default="review")
    parser.add_argument("--reasoning", choices=("high", "max"), default="high")
    parser.add_argument("--profile", default="deepseek-flash")
    parser.add_argument("--timeout", type=int, default=1800, help="Seconds before termination.")
    parser.add_argument("--structured", action="store_true")
    parser.add_argument("--add-dir", action="append", default=[])
    parser.add_argument(
        "--transport",
        choices=("auto", "native", "curl"),
        default="auto",
        help="Use a loopback curl TLS bridge automatically on macOS.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


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
) -> int | None:
    """Stream child stdout to stderr until EOF or the deadline.

    Returns ``None`` on EOF and 124 when the deadline expires first.
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
        print(line, end="", file=sys.stderr, flush=True)


def main() -> int:
    args = parse_args()
    if args.transport == "curl" and platform.system() != "Darwin":
        print("--transport curl is only supported on macOS.", file=sys.stderr)
        return 2
    codex = shutil.which("codex")
    if not codex:
        print("codex executable not found", file=sys.stderr)
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
        scripts_dir = Path(__file__).resolve().parent
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

    sandbox = "read-only" if args.mode == "review" else "workspace-write"
    schema = Path(__file__).resolve().parent.parent / "assets" / "result.schema.json"
    use_curl_bridge = args.transport == "curl" or (
        args.transport == "auto"
        and platform.system() == "Darwin"
        and shutil.which("curl") is not None
    )

    with tempfile.TemporaryDirectory(prefix="deepseek-subagent-") as temp_dir:
        final_path = Path(temp_dir) / "final.txt"
        bridge_process: subprocess.Popen[str] | None = None
        bridge_url: str | None = None
        if use_curl_bridge and not args.dry_run:
            try:
                bridge_process, bridge_url = start_curl_bridge(
                    Path(__file__).resolve().parent
                )
            except RuntimeError as error:
                print(f"Could not start DeepSeek curl bridge: {error}", file=sys.stderr)
                return 2
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
            command.extend(
                ("-c", f'model_providers.deepseek.base_url="{bridge_url}"')
            )
        elif use_curl_bridge and args.dry_run:
            command.extend(
                (
                    "-c",
                    'model_providers.deepseek.base_url="http://127.0.0.1:<port>"',
                )
            )
        for extra in args.add_dir:
            command.extend(("--add-dir", str(Path(extra).expanduser().resolve())))
        if args.structured:
            command.extend(("--output-schema", str(schema)))
        command.append("-")

        if args.dry_run:
            if os.name == "nt":
                print(subprocess.list2cmdline(command))
            else:
                print(shlex.join(command))
            return 0

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **popen_kwargs(),
            )
            assert process.stdin is not None
            assert process.stdout is not None
            process.stdin.write(task)
            process.stdin.close()

            deadline = time.monotonic() + args.timeout
            try:
                timeout_status = stream_child_output(process, deadline)
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
            if not final_path.exists():
                print("DeepSeek subagent produced no final message.", file=sys.stderr)
                return 1

            print(final_path.read_text(encoding="utf-8").strip())
            return 0
        finally:
            if bridge_process is not None:
                terminate_process_tree(bridge_process)


if __name__ == "__main__":
    raise SystemExit(main())
