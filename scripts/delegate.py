#!/usr/bin/env python3
"""Run DeepSeek V4 Flash as an ephemeral Codex subagent."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
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
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def main() -> int:
    args = parse_args()
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
        key_helper = Path(__file__).resolve().parent / "deepseek_key.py"
        key_check = subprocess.run(
            [sys.executable, str(key_helper)],
            check=False,
            capture_output=True,
            text=True,
        )
        if key_check.returncode != 0 or not key_check.stdout.strip():
            print(
                "DeepSeek API key is unavailable. Run: python3 "
                "~/.codex/skills/delegate-to-deepseek/scripts/setup.py store-key",
                file=sys.stderr,
            )
            return 3

    sandbox = "read-only" if args.mode == "review" else "workspace-write"
    schema = Path(__file__).resolve().parent.parent / "assets" / "result.schema.json"

    with tempfile.TemporaryDirectory(prefix="deepseek-subagent-") as temp_dir:
        final_path = Path(temp_dir) / "final.txt"
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
        for extra in args.add_dir:
            command.extend(("--add-dir", str(Path(extra).expanduser().resolve())))
        if args.structured:
            command.extend(("--output-schema", str(schema)))
        command.append("-")

        if args.dry_run:
            print(shlex.join(command))
            return 0

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(task)
        process.stdin.close()

        deadline = time.monotonic() + args.timeout
        try:
            while process.poll() is None:
                if time.monotonic() >= deadline:
                    print(
                        f"DeepSeek subagent timed out after {args.timeout} seconds.",
                        file=sys.stderr,
                    )
                    terminate_process_group(process)
                    return 124
                readable, _, _ = select.select([process.stdout], [], [], 1.0)
                if readable:
                    line = process.stdout.readline()
                    if line:
                        print(line, end="", file=sys.stderr, flush=True)
            for line in process.stdout:
                print(line, end="", file=sys.stderr, flush=True)
        except KeyboardInterrupt:
            terminate_process_group(process)
            return 130

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


if __name__ == "__main__":
    raise SystemExit(main())
