from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import setup  # noqa: E402


class ClaudeSkillSourceTests(unittest.TestCase):
    def test_repository_ships_both_claude_files(self) -> None:
        source = ROOT / ".claude" / "skills" / "delegate-to-deepseek"
        self.assertTrue((source / "SKILL.md").is_file())
        self.assertTrue((source / "deepseek").is_file())

    def test_wrapper_defaults_to_the_claude_backend(self) -> None:
        wrapper = (ROOT / ".claude/skills/delegate-to-deepseek/deepseek").read_text(
            encoding="utf-8"
        )
        self.assertIn("--backend claude", wrapper)
        self.assertIn("--timeout 480", wrapper)

    def test_claude_skill_frontmatter_names_the_skill(self) -> None:
        body = (ROOT / ".claude/skills/delegate-to-deepseek/SKILL.md").read_text(
            encoding="utf-8"
        )
        self.assertTrue(body.startswith("---\n"))
        self.assertIn("name: delegate-to-deepseek", body)

    def test_both_installed_files_carry_the_overwrite_marker(self) -> None:
        # install_claude() only replaces files containing MARKER_CLAUDE, so the
        # shipped originals must contain it or a reinstall would refuse.
        source = ROOT / ".claude" / "skills" / "delegate-to-deepseek"
        for name in ("SKILL.md", "deepseek"):
            with self.subTest(name=name):
                self.assertIn(
                    setup.MARKER_CLAUDE, (source / name).read_text(encoding="utf-8")
                )


class InstallClaudeTests(unittest.TestCase):
    def install_into(self, home: Path) -> tuple[int, str]:
        output = io.StringIO()
        errors = io.StringIO()
        with mock.patch.object(setup, "claude_home", return_value=home):
            with redirect_stdout(output), redirect_stderr(errors):
                status = setup.install_claude()
        return status, output.getvalue() + errors.getvalue()

    def test_install_copies_both_files_and_marks_the_wrapper_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            status, _ = self.install_into(home)

            self.assertEqual(status, 0)
            target = home / "skills" / "delegate-to-deepseek"
            self.assertTrue((target / "SKILL.md").is_file())
            wrapper = target / "deepseek"
            self.assertTrue(wrapper.is_file())
            self.assertTrue(wrapper.stat().st_mode & 0o111)

    def test_reinstall_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.install_into(home)
            status, message = self.install_into(home)

            self.assertEqual(status, 0)
            self.assertIn("already current", message)

    def test_stale_copy_is_refreshed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.install_into(home)
            installed = home / "skills" / "delegate-to-deepseek" / "SKILL.md"
            installed.write_text(
                "---\nname: delegate-to-deepseek\n---\nstale\n", encoding="utf-8"
            )

            self.assertFalse(self.currentness(home))
            status, _ = self.install_into(home)

            self.assertEqual(status, 0)
            self.assertNotIn("stale", installed.read_text(encoding="utf-8"))
            self.assertTrue(self.currentness(home))

    def test_unmanaged_file_is_never_clobbered(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            target = home / "skills" / "delegate-to-deepseek"
            target.mkdir(parents=True)
            stranger = target / "SKILL.md"
            stranger.write_text("someone else's skill\n", encoding="utf-8")

            status, message = self.install_into(home)

            self.assertEqual(status, 1)
            self.assertIn("Refusing to overwrite", message)
            self.assertEqual(stranger.read_text(encoding="utf-8"), "someone else's skill\n")

    def currentness(self, home: Path) -> bool:
        with mock.patch.object(setup, "claude_home", return_value=home):
            return setup.claude_skill_is_current()

    def test_currentness_is_false_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(self.currentness(Path(temp)))

    def test_currentness_is_true_after_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            self.install_into(home)
            self.assertTrue(self.currentness(home))


class ClaudeHomeTests(unittest.TestCase):
    def test_claude_config_dir_overrides_the_default(self) -> None:
        with mock.patch.dict(setup.os.environ, {"CLAUDE_CONFIG_DIR": "/tmp/cc"}):
            self.assertEqual(setup.claude_home(), Path("/tmp/cc"))

    def test_default_is_dot_claude_in_home(self) -> None:
        with mock.patch.dict(setup.os.environ, {}, clear=True):
            self.assertEqual(setup.claude_home(), Path.home() / ".claude")


if __name__ == "__main__":
    unittest.main()
