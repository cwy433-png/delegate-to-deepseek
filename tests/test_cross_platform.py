from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import deepseek_key  # noqa: E402
import delegate  # noqa: E402
import setup  # noqa: E402
import win_cred  # noqa: E402


class ProfileTests(unittest.TestCase):
    def test_toml_escapes_windows_paths_and_uses_python_command(self) -> None:
        profile = setup._profile_text(
            Path(r'C:\Users\A B\models "flash".json'),
            Path(r"C:\Users\A B\deepseek_key.py"),
            Path(r"C:\Program Files\Python\python.exe"),
        )

        self.assertIn(
            'model_catalog_json = "C:\\\\Users\\\\A B\\\\models \\"flash\\".json"',
            profile,
        )
        self.assertIn(
            'command = "C:\\\\Program Files\\\\Python\\\\python.exe"',
            profile,
        )
        self.assertIn(
            'args = ["C:\\\\Users\\\\A B\\\\deepseek_key.py"]',
            profile,
        )

    def test_control_characters_are_escaped(self) -> None:
        self.assertEqual(setup.toml_basic_string("a\tb\nc"), '"a\\tb\\nc"')


class CredentialTests(unittest.TestCase):
    def test_environment_key_takes_precedence(self) -> None:
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": " sk-test "}), mock.patch.object(
            deepseek_key, "native_key", side_effect=AssertionError("must not run")
        ), redirect_stdout(output):
            status = deepseek_key.main()

        self.assertEqual(status, 0)
        self.assertEqual(output.getvalue(), "sk-test\n")

    def test_win_cred_import_is_inert_off_windows(self) -> None:
        if os.name != "nt":
            with self.assertRaises(OSError):
                win_cred.read_credential("codex-test")

    def test_write_credential_uses_atomic_credwrite(self) -> None:
        class FakeAdvapi:
            def __init__(self) -> None:
                self.blob_size = 0

            def CredWriteW(self, pointer: object, flags: int) -> int:
                self.blob_size = pointer._obj.CredentialBlobSize  # type: ignore[attr-defined]
                self.asserted_flags = flags
                return 1

        fake = FakeAdvapi()
        with mock.patch.object(win_cred, "_advapi32", return_value=fake):
            win_cred.write_credential("codex-test", "sk-test", user="tester")

        self.assertEqual(fake.blob_size, len("sk-test".encode("utf-16-le")))
        self.assertEqual(fake.asserted_flags, 0)

    def test_windows_dialog_passes_no_key_in_process_arguments(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout="sk-test", stderr="")
        with mock.patch.object(setup.shutil, "which", return_value="powershell.exe"), mock.patch.object(
            setup.subprocess, "run", return_value=completed
        ) as run:
            key, status = setup.prompt_for_key_windows()

        command = run.call_args.args[0]
        self.assertEqual((key, status), ("sk-test", 0))
        self.assertNotIn("sk-test", " ".join(command))
        self.assertIn("-STA", command)

    def test_windows_dialog_distinguishes_cancel(self) -> None:
        completed = subprocess.CompletedProcess([], 130, stdout="", stderr="")
        with mock.patch.object(setup.shutil, "which", return_value="powershell.exe"), mock.patch.object(
            setup.subprocess, "run", return_value=completed
        ):
            self.assertEqual(setup.prompt_for_key_windows(), (None, 130))


class DelegateTests(unittest.TestCase):
    def test_windows_process_group_flags(self) -> None:
        with mock.patch.object(delegate.os, "name", "nt"), mock.patch.object(
            delegate.subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            512,
            create=True,
        ):
            self.assertEqual(delegate.popen_kwargs(), {"creationflags": 512})

    def test_output_pump_streams_lines_without_select(self) -> None:
        class FakeProcess:
            stdout = io.StringIO("first\nsecond\n")

            @staticmethod
            def poll() -> int:
                return 0

        error_output = io.StringIO()
        collected: list[str] = []
        with redirect_stderr(error_output):
            status = delegate.stream_child_output(
                FakeProcess(), time.monotonic() + 2, collected
            )

        self.assertIsNone(status)
        self.assertEqual(error_output.getvalue(), "first\nsecond\n")
        # The claude backend recovers its final answer from these lines.
        self.assertEqual(collected, ["first\n", "second\n"])

    def test_windows_termination_uses_taskkill_tree(self) -> None:
        process = mock.Mock(pid=1234)
        process.poll.return_value = None
        with mock.patch.object(delegate.os, "name", "nt"), mock.patch.object(
            delegate.subprocess, "run"
        ) as run:
            delegate.terminate_process_tree(process)

        run.assert_called_once_with(
            ["taskkill", "/PID", "1234", "/T", "/F"],
            check=False,
            capture_output=True,
        )
        process.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
