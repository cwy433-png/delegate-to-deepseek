from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import delegate  # noqa: E402


ASSETS = Path(__file__).resolve().parents[1] / "assets"
SCHEMA = ASSETS / "result.schema.json"


def make_args(**overrides: object) -> argparse.Namespace:
    defaults = dict(
        task="t",
        cwd=os.getcwd(),
        backend="claude",
        mode="review",
        reasoning="high",
        model="deepseek-v4-flash",
        profile="deepseek-flash",
        timeout=1800,
        structured=False,
        add_dir=[],
        shell=False,
        transport="auto",
        dry_run=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def build(**overrides: object) -> list[str]:
    with mock.patch.object(delegate.shutil, "which", return_value="/bin/claude"):
        return delegate.build_claude_plan(make_args(**overrides), SCRIPTS, SCHEMA).command


def flag(command: list[str], name: str) -> str:
    return command[command.index(name) + 1]


class ToolBoundaryTests(unittest.TestCase):
    def test_review_grants_no_shell_and_no_blanket_approval(self) -> None:
        command = build(mode="review")
        self.assertEqual(flag(command, "--tools"), "Read,Grep,Glob")
        self.assertNotIn("--allowedTools", command)
        self.assertEqual(flag(command, "--permission-mode"), "dontAsk")

    def test_review_shell_adds_bash_without_pre_approving_it(self) -> None:
        # Bash is available but unapproved, so the built-in classifier auto-runs
        # read-only commands and denies anything that could mutate the workspace.
        command = build(mode="review", shell=True)
        self.assertEqual(flag(command, "--tools"), "Read,Grep,Glob,Bash")
        self.assertNotIn("--allowedTools", command)

    def test_write_pre_approves_the_tools_it_grants(self) -> None:
        # Without --allowedTools the child stalls retrying denied commands.
        command = build(mode="write")
        self.assertEqual(flag(command, "--tools"), "Read,Grep,Glob,Edit,Write,Bash")
        self.assertEqual(flag(command, "--allowedTools"), "Bash,Edit,Write")
        self.assertEqual(flag(command, "--permission-mode"), "acceptEdits")

    def test_task_tool_is_never_granted(self) -> None:
        for mode in ("review", "write"):
            self.assertNotIn("Task", flag(build(mode=mode), "--tools").split(","))

    def test_recursion_into_this_skill_is_disabled(self) -> None:
        self.assertIn("--disable-slash-commands", build())


class CredentialIsolationTests(unittest.TestCase):
    def test_key_travels_by_helper_not_argv_or_env(self) -> None:
        command = build()
        settings = json.loads(flag(command, "--settings"))
        self.assertIn("deepseek_key.py", settings["apiKeyHelper"])
        self.assertNotIn("sk-", " ".join(command))

    def test_bare_blocks_oauth_and_keychain_fallback(self) -> None:
        self.assertIn("--bare", build())

    def test_child_env_drops_inherited_anthropic_credentials(self) -> None:
        polluted = {
            "ANTHROPIC_API_KEY": "sk-ant-parent",
            "ANTHROPIC_AUTH_TOKEN": "parent-token",
            "ANTHROPIC_MODEL": "claude-opus-5",
            "CLAUDE_CODE_SIMPLE": "1",
            "DEEPSEEK_API_KEY": "sk-deepseek",
            "PATH": "/usr/bin",
        }
        with mock.patch.dict(delegate.os.environ, polluted, clear=True):
            env = delegate.claude_child_env()

        self.assertEqual(env["ANTHROPIC_BASE_URL"], delegate.ANTHROPIC_BASE_URL)
        for leaked in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
            self.assertNotIn(leaked, env)
        self.assertNotIn("CLAUDE_CODE_SIMPLE", env)
        # The key helper reads DEEPSEEK_API_KEY, so it has to survive.
        self.assertEqual(env["DEEPSEEK_API_KEY"], "sk-deepseek")


class StructuredOutputTests(unittest.TestCase):
    def test_schema_meta_reference_is_stripped_for_claude(self) -> None:
        # Codex resolves draft 2020-12; Claude Code rejects the meta-ref.
        self.assertIn("$schema", SCHEMA.read_text(encoding="utf-8"))
        document = json.loads(delegate.claude_json_schema(SCHEMA))
        self.assertNotIn("$schema", document)
        self.assertEqual(document["required"][0], "status")

    def test_structured_run_passes_the_schema_inline(self) -> None:
        command = build(structured=True)
        self.assertNotIn("$schema", flag(command, "--json-schema"))


class FinalAnswerTests(unittest.TestCase):
    def extract(self, lines: list[str]) -> str | None:
        with mock.patch.object(delegate.shutil, "which", return_value="/bin/claude"):
            plan = delegate.build_claude_plan(make_args(), SCRIPTS, SCHEMA)
        return plan.extract_final(lines)

    def test_result_event_wins_over_earlier_events(self) -> None:
        lines = [
            json.dumps({"type": "assistant", "result": "not this one"}) + "\n",
            json.dumps({"type": "result", "is_error": False, "result": " done \n"}) + "\n",
        ]
        self.assertEqual(self.extract(lines), "done")

    def test_error_result_yields_no_final_answer(self) -> None:
        lines = [json.dumps({"type": "result", "is_error": True, "subtype": "e"}) + "\n"]
        self.assertIsNone(self.extract(lines))

    def test_malformed_lines_are_skipped(self) -> None:
        lines = [
            "not json but mentions \"result\"\n",
            json.dumps({"type": "result", "is_error": False, "result": "ok"}) + "\n",
        ]
        self.assertEqual(self.extract(lines), "ok")

    def test_missing_result_event_yields_none(self) -> None:
        self.assertIsNone(self.extract([json.dumps({"type": "system"}) + "\n"]))


class MissingKeySetupTests(unittest.TestCase):
    """A missing key opens the key window, but must not install extra state."""

    def setup_action_for(self, backend: str) -> str:
        no_key = subprocess.CompletedProcess([], returncode=1, stdout="", stderr="")
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append([str(part) for part in command])
            return no_key

        argv = ["delegate.py", "--backend", backend, "--task", "t", "--cwd", os.getcwd()]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            delegate.subprocess, "run", side_effect=fake_run
        ):
            delegate.main()

        setup_calls = [call for call in calls if call[1].endswith("setup.py")]
        self.assertTrue(setup_calls, "setup.py was never invoked")
        return setup_calls[0][2]

    def test_claude_backend_only_stores_the_key(self) -> None:
        self.assertEqual(self.setup_action_for("claude"), "store-key")

    def test_codex_backend_also_installs_the_profile(self) -> None:
        self.assertEqual(self.setup_action_for("codex"), "configure")


class BackendGuardTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> int:
        with mock.patch.object(sys, "argv", ["delegate.py", *argv]):
            return delegate.main()

    def test_curl_transport_is_rejected_for_the_claude_backend(self) -> None:
        status = self.run_main(
            ["--backend", "claude", "--transport", "curl", "--task", "t", "--dry-run"]
        )
        self.assertEqual(status, 2)

    def test_shell_flag_is_rejected_for_the_codex_backend(self) -> None:
        status = self.run_main(["--backend", "codex", "--shell", "--task", "t", "--dry-run"])
        self.assertEqual(status, 2)


if __name__ == "__main__":
    unittest.main()
