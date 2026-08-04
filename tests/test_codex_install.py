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


def install_into(home: Path) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}):
        with redirect_stdout(out), redirect_stderr(err):
            status = setup.install_codex()
    return status, out.getvalue(), err.getvalue()


class CodexSkillSourceTests(unittest.TestCase):
    def test_repository_ships_every_installed_file(self) -> None:
        for name in setup.CODEX_SKILL_FILES:
            self.assertTrue((ROOT / name).is_file(), f"missing source file: {name}")

    def test_development_only_files_are_not_installed(self) -> None:
        # setup.py, the tests, and the other harnesses' trees are how the skill
        # is developed. Shipping them would recreate the coupling that moving
        # the repository out of ~/.codex was meant to remove.
        for name in ("scripts/setup.py", "AGENTS.md", "README.md"):
            self.assertNotIn(name, setup.CODEX_SKILL_FILES)


class ProfileTargetTests(unittest.TestCase):
    def test_profile_points_at_the_installed_copy_not_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}):
                text = setup.profile_text()
                installed = setup.codex_skill_target()
        self.assertIn(str(installed / "assets" / "models.json"), text)
        self.assertIn(str(installed / "scripts" / "deepseek_key.py"), text)
        # The working tree must not leak into a profile: the repository may be
        # moved or deleted without breaking a working Codex install.
        self.assertNotIn(str(ROOT / "assets" / "models.json"), text)


class InstallCodexTests(unittest.TestCase):
    def test_install_creates_every_file_and_the_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            status, out, _ = install_into(home)
            self.assertEqual(status, 0)
            self.assertIn("Installed Codex skill", out)

            target = home / "skills" / setup.SKILL_NAME
            self.assertTrue((target / setup.CODEX_SKILL_STAMP).is_file())
            for name in setup.CODEX_SKILL_FILES:
                self.assertEqual(
                    (target / name).read_text(encoding="utf-8"),
                    (ROOT / name).read_text(encoding="utf-8"),
                    f"content differs: {name}",
                )

    def test_second_install_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            install_into(home)
            status, out, _ = install_into(home)
            self.assertEqual(status, 0)
            self.assertIn("already current", out)

    def test_install_refuses_an_unmanaged_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            target = home / "skills" / setup.SKILL_NAME
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text("someone else's skill", encoding="utf-8")

            status, _, err = install_into(home)
            self.assertEqual(status, 1)
            self.assertIn("Refusing to overwrite unmanaged directory", err)
            self.assertEqual(
                (target / "SKILL.md").read_text(encoding="utf-8"),
                "someone else's skill",
            )

    def test_install_refuses_to_target_the_repository_itself(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(setup, "codex_skill_target", return_value=ROOT):
            with redirect_stdout(out), redirect_stderr(err):
                status = setup.install_codex()
        self.assertEqual(status, 1)
        self.assertIn("Refusing to install onto the repository itself", err.getvalue())


class CodexSkillDriftTests(unittest.TestCase):
    def test_missing_install_is_not_current(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with mock.patch.dict("os.environ", {"CODEX_HOME": raw}):
                self.assertFalse(setup.codex_skill_is_current())

    def test_fresh_install_is_current(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            install_into(home)
            with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}):
                self.assertTrue(setup.codex_skill_is_current())

    def test_edited_install_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            install_into(home)
            target = home / "skills" / setup.SKILL_NAME
            (target / "SKILL.md").write_text("drifted", encoding="utf-8")
            with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}):
                self.assertFalse(setup.codex_skill_is_current())

    def test_a_stamp_removed_by_hand_makes_it_stale(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            home = Path(raw)
            install_into(home)
            (home / "skills" / setup.SKILL_NAME / setup.CODEX_SKILL_STAMP).unlink()
            with mock.patch.dict("os.environ", {"CODEX_HOME": str(home)}):
                self.assertFalse(setup.codex_skill_is_current())


if __name__ == "__main__":
    unittest.main()
