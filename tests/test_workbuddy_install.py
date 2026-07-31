from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import setup  # noqa: E402


FAKE_KEY = "sk-not-a-real-key"


class ModelDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = json.loads(
            (ROOT / ".codebuddy" / "models.json").read_text(encoding="utf-8")
        )
        self.model = self.document["models"][0]

    def test_id_matches_the_id_the_installer_maps_to(self) -> None:
        self.assertEqual(self.model["id"], setup.WORKBUDDY_MODEL_ID)

    def test_points_at_deepseek_directly(self) -> None:
        self.assertEqual(self.model["url"], "https://api.deepseek.com")

    def test_key_is_an_env_reference_never_a_literal(self) -> None:
        # The file is committed, so a literal key here would leak on push.
        self.assertEqual(self.model["apiKey"], "${DEEPSEEK_API_KEY}")

    def test_tool_calling_is_declared(self) -> None:
        # Sub-agents are useless without it, and the live API was verified to
        # return finish_reason "tool_calls" on this path.
        self.assertTrue(self.model["supportsToolCall"])


class NoCommittedSecretTests(unittest.TestCase):
    # A real key is "sk-" plus a long opaque run. Matching only that shape keeps
    # prose, input placeholders, and the "must start with sk-" validator out of
    # the results, so the check stays worth reading when it fires.
    KEY_SHAPE = re.compile(r"sk-[A-Za-z0-9]{24,}")
    PLACEHOLDERS = ("not-a-real", "xxxx", "your", "example")

    def test_no_tracked_file_contains_a_deepseek_key(self) -> None:
        found = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.relative_to(ROOT).parts:
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for match in self.KEY_SHAPE.finditer(body):
                token = match.group(0)
                if any(hint in token.lower() for hint in self.PLACEHOLDERS):
                    continue
                found.append(f"{path.relative_to(ROOT)}: {token[:8]}…")
        self.assertEqual(found, [], "a credential-shaped string is committed")


class DocumentSeparationTests(unittest.TestCase):
    """Each harness should only load what applies to it."""

    def setUp(self) -> None:
        self.agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.codebuddy = (ROOT / "CODEBUDDY.md").read_text(encoding="utf-8")

    def test_shared_file_carries_no_claude_only_detail(self) -> None:
        for token in ("--bare", "ANTHROPIC_", "install-claude", "allowedTools"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.agents)

    def test_shared_file_carries_no_workbuddy_only_detail(self) -> None:
        for token in ("install-workbuddy", "CODEBUDDY_SMALL_FAST_MODEL", "workbuddy"):
            with self.subTest(token=token):
                self.assertNotIn(token, self.agents)

    def test_claude_file_keeps_the_credential_invariant(self) -> None:
        self.assertIn("--bare", self.claude)
        self.assertIn("ANTHROPIC_", self.claude)

    def test_claude_file_imports_the_shared_one(self) -> None:
        self.assertIn("@AGENTS.md", self.claude)

    def test_workbuddy_file_states_the_plaintext_downgrade(self) -> None:
        # The one place this design is weaker than the rest of the repository,
        # so it must not quietly disappear from the guide.
        self.assertIn("plaintext", self.codebuddy)


class WorkBuddyInstallTests(unittest.TestCase):
    def install_into(self, home: Path) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(setup, "workbuddy_home", return_value=home), \
                mock.patch.object(setup, "stored_key", return_value=FAKE_KEY):
            with redirect_stdout(out), redirect_stderr(err):
                status = setup.install_workbuddy()
        return status, out.getvalue() + err.getvalue()

    def make_home(self, settings: object | None) -> Path:
        home = Path(tempfile.mkdtemp())
        if settings is not None:
            body = settings if isinstance(settings, str) else json.dumps(settings)
            (home / "settings.json").write_text(body, encoding="utf-8")
        return home

    def read(self, home: Path) -> dict:
        return json.loads((home / "settings.json").read_text(encoding="utf-8"))

    def test_unrelated_settings_survive_the_merge(self) -> None:
        home = self.make_home(
            {"enabledPlugins": {"a@b": True}, "claw": {"legacyOwnerUid": "1"}}
        )
        status, _ = self.install_into(home)

        self.assertEqual(status, 0)
        settings = self.read(home)
        self.assertEqual(settings["enabledPlugins"], {"a@b": True})
        self.assertEqual(settings["claw"], {"legacyOwnerUid": "1"})
        self.assertEqual(settings["env"]["DEEPSEEK_API_KEY"], FAKE_KEY)
        self.assertEqual(
            settings["env"]["CODEBUDDY_SMALL_FAST_MODEL"], setup.WORKBUDDY_MODEL_ID
        )

    def test_existing_env_entries_survive(self) -> None:
        home = self.make_home({"env": {"SOMETHING_ELSE": "keep me"}})
        self.install_into(home)
        self.assertEqual(self.read(home)["env"]["SOMETHING_ELSE"], "keep me")

    def test_settings_file_is_owner_only(self) -> None:
        home = self.make_home({})
        self.install_into(home)
        self.assertEqual((home / "settings.json").stat().st_mode & 0o777, 0o600)

    def test_reinstall_is_idempotent(self) -> None:
        home = self.make_home({})
        self.install_into(home)
        status, message = self.install_into(home)
        self.assertEqual(status, 0)
        self.assertIn("already current", message)

    def test_unparsable_settings_are_never_rewritten(self) -> None:
        home = self.make_home("{not json")
        status, message = self.install_into(home)

        self.assertEqual(status, 1)
        self.assertIn("Refusing", message)
        self.assertEqual((home / "settings.json").read_text(encoding="utf-8"), "{not json")

    def test_non_object_env_is_never_replaced(self) -> None:
        home = self.make_home({"env": "surprise"})
        status, message = self.install_into(home)

        self.assertEqual(status, 1)
        self.assertIn("Refusing", message)
        self.assertEqual(self.read(home)["env"], "surprise")

    def test_missing_config_directory_is_reported_not_created(self) -> None:
        home = Path(tempfile.mkdtemp()) / "never-launched"
        status, message = self.install_into(home)

        self.assertEqual(status, 1)
        self.assertIn("not found", message)
        self.assertFalse(home.exists())

    def test_currentness_reflects_the_installed_state(self) -> None:
        home = self.make_home({})
        with mock.patch.object(setup, "workbuddy_home", return_value=home):
            self.assertFalse(setup.workbuddy_env_is_current())
        self.install_into(home)
        with mock.patch.object(setup, "workbuddy_home", return_value=home):
            self.assertTrue(setup.workbuddy_env_is_current())


class WindowsWrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.source = ROOT / ".claude" / "skills" / "delegate-to-deepseek"

    def test_both_wrappers_ship(self) -> None:
        self.assertTrue((self.source / "deepseek").is_file())
        self.assertTrue((self.source / "deepseek.cmd").is_file())

    def test_installer_copies_both(self) -> None:
        names = [name for name, _ in setup.CLAUDE_SKILL_FILES]
        self.assertIn("deepseek", names)
        self.assertIn("deepseek.cmd", names)

    def test_only_the_posix_wrapper_is_marked_executable(self) -> None:
        modes = dict(setup.CLAUDE_SKILL_FILES)
        self.assertTrue(modes["deepseek"])
        self.assertFalse(modes["deepseek.cmd"])

    def test_wrappers_agree_on_defaults(self) -> None:
        for name in ("deepseek", "deepseek.cmd"):
            body = (self.source / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertIn("--timeout 480", body)
                self.assertIn("--backend claude", body)

    def test_windows_wrapper_avoids_hardcoded_posix_paths(self) -> None:
        body = (self.source / "deepseek.cmd").read_text(encoding="utf-8")
        self.assertNotIn("/Applications/", body)
        self.assertIn("%USERPROFILE%", body)


if __name__ == "__main__":
    unittest.main()
