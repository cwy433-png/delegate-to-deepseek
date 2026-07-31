from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import app_server  # noqa: E402


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()

    @staticmethod
    def poll() -> None:
        return None


class ConfigTests(unittest.TestCase):
    def test_gui_config_is_deepseek_only_and_contains_no_key(self) -> None:
        text = app_server.gui_config_text()

        self.assertIn('model = "deepseek-v4-flash"', text)
        self.assertIn('model_provider = "deepseek"', text)
        self.assertIn('wire_api = "responses"', text)
        self.assertIn('web_search = "disabled"', text)
        self.assertIn('enabled = false', text)
        self.assertNotIn("sk-", text)
        self.assertNotIn("DEEPSEEK_API_KEY", text)

    def test_install_gui_config_uses_isolated_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "deepcodex"
            target = app_server.install_gui_config(home)

            self.assertEqual(target, home.resolve() / "config.toml")
            self.assertEqual(target.read_text(encoding="utf-8"), app_server.gui_config_text())


class ProtocolTests(unittest.TestCase):
    def make_client(self) -> app_server.AppServerClient:
        client = app_server.AppServerClient(
            codex="codex-test", use_curl_bridge=False
        )
        client.process = FakeProcess()  # type: ignore[assignment]
        return client

    def test_notify_serializes_one_json_object_per_line(self) -> None:
        client = self.make_client()

        client.notify("initialized", {})

        assert client.process is not None and client.process.stdin is not None
        self.assertEqual(
            json.loads(client.process.stdin.getvalue()),
            {"method": "initialized"},
        )

    def test_request_matches_response_by_id(self) -> None:
        client = self.make_client()

        def reply() -> None:
            while True:
                assert client.process is not None and client.process.stdin is not None
                line = client.process.stdin.getvalue()
                if line:
                    request_id = json.loads(line)["id"]
                    client.handle_message({"id": request_id, "result": {"ok": True}})
                    return

        worker = threading.Thread(target=reply, daemon=True)
        worker.start()
        result = client.request("test/method", {"value": 1}, timeout=1)

        self.assertEqual(result, {"ok": True})

    def test_server_request_is_forwarded_for_gui_approval(self) -> None:
        events: list[dict[str, object]] = []
        client = app_server.AppServerClient(
            codex="codex-test", use_curl_bridge=False, event_handler=events.append
        )
        message = {
            "id": 41,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "git push"},
        }

        client.handle_message(message)

        self.assertEqual(events, [{"type": "server_request", "message": message}])

    def test_unsupported_server_request_can_be_rejected_as_json_rpc_error(self) -> None:
        client = self.make_client()

        client.respond_error(12, "unsupported")

        assert client.process is not None and client.process.stdin is not None
        self.assertEqual(
            json.loads(client.process.stdin.getvalue()),
            {
                "id": 12,
                "error": {"code": -32601, "message": "unsupported"},
            },
        )

    def test_start_thread_pins_deepseek_and_workspace_permissions(self) -> None:
        client = self.make_client()
        captured: dict[str, object] = {}

        def fake_request(
            method: str,
            params: dict[str, object],
            *,
            timeout: float = 30.0,
        ) -> dict[str, object]:
            captured.update({"method": method, "params": params, "timeout": timeout})
            return {"thread": {"id": "thread-1"}}

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            client, "request", side_effect=fake_request
        ):
            thread_id = client.start_thread(Path(directory))

        self.assertEqual(thread_id, "thread-1")
        self.assertEqual(captured["method"], "thread/start")
        params = captured["params"]
        assert isinstance(params, dict)
        self.assertEqual(params["model"], "deepseek-v4-flash")
        self.assertEqual(params["modelProvider"], "deepseek")
        self.assertEqual(params["approvalPolicy"], "on-request")
        self.assertEqual(params["sandbox"], "workspace-write")

    def test_start_turn_uses_v2_text_input_shape(self) -> None:
        client = self.make_client()
        client.thread_id = "thread-1"
        with mock.patch.object(
            client,
            "request",
            return_value={"turn": {"id": "turn-1"}},
        ) as request:
            turn_id = client.start_turn("inspect this project")

        self.assertEqual(turn_id, "turn-1")
        request.assert_called_once_with(
            "turn/start",
            {
                "threadId": "thread-1",
                "input": [{"type": "text", "text": "inspect this project"}],
            },
            timeout=30.0,
        )


if __name__ == "__main__":
    unittest.main()
