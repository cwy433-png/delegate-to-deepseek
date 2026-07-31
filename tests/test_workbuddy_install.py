from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import re
import shutil
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
        self.assertEqual(
            self.model["url"],
            "https://api.deepseek.com/v1/chat/completions",
        )

    def test_id_is_the_real_api_model_slug(self) -> None:
        self.assertEqual(self.model["id"], "deepseek-v4-flash")

    def test_model_is_exposed_in_the_global_picker(self) -> None:
        self.assertIn(self.model["id"], self.document["availableModels"])

    def test_key_is_an_env_reference_never_a_literal(self) -> None:
        # The file is committed, so a literal key here would leak on push.
        self.assertEqual(self.model["apiKey"], "${DEEPSEEK_API_KEY}")

    def test_tool_calling_is_declared(self) -> None:
        # Sub-agents are useless without it, and the live API was verified to
        # return finish_reason "tool_calls" on this path.
        self.assertTrue(self.model["supportsToolCall"])


class WorkBuddyAgentDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.body = (ROOT / ".codebuddy" / "agents" / "deepseek.md").read_text(
            encoding="utf-8"
        )

    def test_agent_is_pinned_to_the_direct_model(self) -> None:
        self.assertRegex(self.body, r"(?m)^name: deepseek$")
        self.assertRegex(
            self.body,
            rf"(?m)^model: {re.escape(setup.WORKBUDDY_AGENT_MODEL_ID)}$",
        )

    def test_agent_is_managed_and_forbids_nested_delegation(self) -> None:
        self.assertIn(setup.MARKER_WORKBUDDY_AGENT, self.body)
        self.assertRegex(
            self.body,
            r"Do not spawn or delegate\s+to other agents",
        )
        tools_line = re.search(r"(?m)^tools: (.+)$", self.body)
        self.assertIsNotNone(tools_line)
        self.assertNotRegex(tools_line.group(1), r"\b(?:Agent|Task|Delegate)\b")


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
        for token in ("install-workbuddy", "CODEBUDDY_SMALL_FAST_MODEL", "settings.json"):
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

    def test_workbuddy_file_imports_the_shared_one(self) -> None:
        self.assertIn("@AGENTS.md", self.codebuddy)


class WorkBuddyInstallTests(unittest.TestCase):
    def install_into(self, home: Path, codebuddy: Path) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(setup, "workbuddy_home", return_value=home), \
                mock.patch.object(setup, "codebuddy_home", return_value=codebuddy), \
                mock.patch.object(setup, "stored_key", return_value=FAKE_KEY):
            with redirect_stdout(out), redirect_stderr(err):
                status = setup.install_workbuddy()
        return status, out.getvalue() + err.getvalue()

    def make_homes(self, settings: object | None) -> tuple[Path, Path]:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        home = root / "workbuddy"
        codebuddy = root / "codebuddy"
        home.mkdir()
        if settings is not None:
            body = settings if isinstance(settings, str) else json.dumps(settings)
            (home / "settings.json").write_text(body, encoding="utf-8")
        return home, codebuddy

    def read(self, home: Path) -> dict:
        return json.loads((home / "settings.json").read_text(encoding="utf-8"))

    def test_unrelated_settings_survive_the_merge(self) -> None:
        home, codebuddy = self.make_homes(
            {"enabledPlugins": {"a@b": True}, "claw": {"legacyOwnerUid": "1"}}
        )
        status, _ = self.install_into(home, codebuddy)

        self.assertEqual(status, 0)
        settings = self.read(home)
        self.assertEqual(settings["enabledPlugins"], {"a@b": True})
        self.assertEqual(settings["claw"], {"legacyOwnerUid": "1"})
        self.assertEqual(settings["env"]["DEEPSEEK_API_KEY"], FAKE_KEY)
        self.assertEqual(
            settings["env"]["CODEBUDDY_SMALL_FAST_MODEL"],
            setup.WORKBUDDY_AGENT_MODEL_ID,
        )
        models = json.loads((codebuddy / "models.json").read_text(encoding="utf-8"))
        self.assertIn(setup.WORKBUDDY_MODEL_ID, models["availableModels"])
        self.assertTrue((codebuddy / "agents" / "deepseek.md").is_file())

    def test_existing_env_entries_survive(self) -> None:
        home, codebuddy = self.make_homes({"env": {"SOMETHING_ELSE": "keep me"}})
        self.install_into(home, codebuddy)
        self.assertEqual(self.read(home)["env"]["SOMETHING_ELSE"], "keep me")

    def test_existing_node_options_survive_on_macos(self) -> None:
        home, codebuddy = self.make_homes(
            {"env": {"NODE_OPTIONS": "--max-old-space-size=4096"}}
        )
        with mock.patch.object(setup.platform, "system", return_value="Darwin"):
            status, _ = self.install_into(home, codebuddy)

        self.assertEqual(status, 0)
        options = self.read(home)["env"]["NODE_OPTIONS"].split()
        self.assertIn("--max-old-space-size=4096", options)
        self.assertIn(setup.WORKBUDDY_MACOS_NODE_OPTION, options)

    def test_non_string_node_options_are_not_overwritten(self) -> None:
        home, codebuddy = self.make_homes({"env": {"NODE_OPTIONS": ["surprise"]}})

        status, message = self.install_into(home, codebuddy)

        self.assertEqual(status, 1)
        self.assertIn("env.NODE_OPTIONS", message)
        self.assertEqual(self.read(home)["env"]["NODE_OPTIONS"], ["surprise"])
        self.assertFalse(codebuddy.exists())

    def test_existing_global_models_and_fields_survive(self) -> None:
        home, codebuddy = self.make_homes({})
        codebuddy.mkdir()
        original = {
            "models": [{"id": "keep-me", "url": "https://example.invalid"}],
            "availableModels": ["keep-me"],
            "customField": {"keep": True},
        }
        (codebuddy / "models.json").write_text(
            json.dumps(original), encoding="utf-8"
        )

        status, _ = self.install_into(home, codebuddy)

        self.assertEqual(status, 0)
        installed = json.loads(
            (codebuddy / "models.json").read_text(encoding="utf-8")
        )
        self.assertEqual(installed["models"][0], original["models"][0])
        self.assertEqual(installed["availableModels"][0], "keep-me")
        self.assertEqual(installed["customField"], {"keep": True})

    def test_conflicting_global_model_is_not_overwritten(self) -> None:
        home, codebuddy = self.make_homes({})
        codebuddy.mkdir()
        conflict = {
            "models": [
                {
                    "id": setup.WORKBUDDY_MODEL_ID,
                    "url": "https://example.invalid/chat/completions",
                }
            ]
        }
        target = codebuddy / "models.json"
        target.write_text(json.dumps(conflict), encoding="utf-8")

        status, message = self.install_into(home, codebuddy)

        self.assertEqual(status, 1)
        self.assertIn("conflicting model", message)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), conflict)
        self.assertFalse((codebuddy / "agents" / "deepseek.md").exists())
        self.assertEqual(self.read(home), {})

    def test_unmanaged_agent_is_not_overwritten(self) -> None:
        home, codebuddy = self.make_homes({})
        agent = codebuddy / "agents" / "deepseek.md"
        agent.parent.mkdir(parents=True)
        agent.write_text("my personal agent\n", encoding="utf-8")

        status, message = self.install_into(home, codebuddy)

        self.assertEqual(status, 1)
        self.assertIn("unmanaged WorkBuddy agent", message)
        self.assertEqual(agent.read_text(encoding="utf-8"), "my personal agent\n")
        self.assertEqual(self.read(home), {})

    def test_settings_file_is_owner_only(self) -> None:
        home, codebuddy = self.make_homes({})
        self.install_into(home, codebuddy)
        self.assertEqual((home / "settings.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((codebuddy / "models.json").stat().st_mode & 0o777, 0o600)

    def test_reinstall_is_idempotent(self) -> None:
        home, codebuddy = self.make_homes({})
        self.install_into(home, codebuddy)
        status, message = self.install_into(home, codebuddy)
        self.assertEqual(status, 0)
        self.assertIn("already current", message)

    def test_managed_small_fast_override_is_normalized(self) -> None:
        for model_id in setup.MANAGED_WORKBUDDY_LITE_MODEL_IDS:
            with self.subTest(model_id=model_id):
                home, codebuddy = self.make_homes(
                    {
                        "env": {
                            "DEEPSEEK_API_KEY": FAKE_KEY,
                            "CODEBUDDY_SMALL_FAST_MODEL": model_id,
                        }
                    }
                )

                status, _ = self.install_into(home, codebuddy)

                self.assertEqual(status, 0)
                self.assertEqual(
                    self.read(home)["env"]["CODEBUDDY_SMALL_FAST_MODEL"],
                    setup.WORKBUDDY_AGENT_MODEL_ID,
                )

    def test_current_settings_permissions_are_still_repaired(self) -> None:
        home, codebuddy = self.make_homes(
            {
                "env": {
                    "DEEPSEEK_API_KEY": FAKE_KEY,
                    "CODEBUDDY_SMALL_FAST_MODEL": setup.WORKBUDDY_AGENT_MODEL_ID,
                }
            }
        )
        target = home / "settings.json"
        target.chmod(0o644)

        status, _ = self.install_into(home, codebuddy)

        self.assertEqual(status, 0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_current_global_model_permissions_are_still_repaired(self) -> None:
        home, codebuddy = self.make_homes({})
        codebuddy.mkdir()
        target = codebuddy / "models.json"
        target.write_text(
            (ROOT / ".codebuddy" / "models.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        target.chmod(0o644)

        status, _ = self.install_into(home, codebuddy)

        self.assertEqual(status, 0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_unparsable_settings_are_never_rewritten(self) -> None:
        home, codebuddy = self.make_homes("{not json")
        status, message = self.install_into(home, codebuddy)

        self.assertEqual(status, 1)
        self.assertIn("Refusing", message)
        self.assertEqual((home / "settings.json").read_text(encoding="utf-8"), "{not json")
        self.assertFalse(codebuddy.exists())

    def test_non_object_env_is_never_replaced(self) -> None:
        home, codebuddy = self.make_homes({"env": "surprise"})
        status, message = self.install_into(home, codebuddy)

        self.assertEqual(status, 1)
        self.assertIn("Refusing", message)
        self.assertEqual(self.read(home)["env"], "surprise")
        self.assertFalse(codebuddy.exists())

    def test_missing_config_directory_is_reported_not_created(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        home = root / "never-launched"
        codebuddy = root / "codebuddy"
        status, message = self.install_into(home, codebuddy)

        self.assertEqual(status, 1)
        self.assertIn("not found", message)
        self.assertFalse(home.exists())
        self.assertFalse(codebuddy.exists())

    def test_currentness_reflects_the_installed_state(self) -> None:
        home, codebuddy = self.make_homes({})
        with mock.patch.object(setup, "workbuddy_home", return_value=home), \
                mock.patch.object(setup, "codebuddy_home", return_value=codebuddy):
            self.assertFalse(setup.workbuddy_env_is_current())
            self.assertFalse(setup.workbuddy_model_is_current())
            self.assertFalse(setup.workbuddy_agent_is_current())
        self.install_into(home, codebuddy)
        with mock.patch.object(setup, "workbuddy_home", return_value=home), \
                mock.patch.object(setup, "codebuddy_home", return_value=codebuddy):
            self.assertTrue(setup.workbuddy_env_is_current())
            self.assertTrue(setup.workbuddy_model_is_current())
            self.assertTrue(setup.workbuddy_agent_is_current())


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
