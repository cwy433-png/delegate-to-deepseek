from __future__ import annotations

import http.client
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import web_gui  # noqa: E402


class RuntimeStateTests(unittest.TestCase):
    def test_event_stream_is_ordered_and_incremental(self) -> None:
        state = web_gui.RuntimeState()
        state.emit("first", value=1)
        state.emit("second", value=2)

        self.assertEqual(
            state.events_after(1),
            [{"seq": 2, "type": "second", "value": 2}],
        )

    def test_turn_completion_clears_busy_state(self) -> None:
        state = web_gui.RuntimeState()
        state.busy = True

        state.on_client_event(
            {
                "type": "notification",
                "message": {
                    "method": "turn/completed",
                    "params": {"turn": {"status": "completed"}},
                },
            }
        )

        self.assertFalse(state.busy)
        self.assertEqual(state.events_after(0)[0]["type"], "notification")

    def test_unsupported_reverse_request_is_rejected_as_protocol_error(self) -> None:
        state = web_gui.RuntimeState()
        client = mock.Mock()
        state.client = client

        state.on_client_event(
            {
                "type": "server_request",
                "message": {
                    "id": 19,
                    "method": "item/tool/requestUserInput",
                    "params": {},
                },
            }
        )

        client.respond_error.assert_called_once_with(
            19,
            "DeepCodex Preview does not support server request item/tool/requestUserInput",
        )
        self.assertEqual(state.events_after(0)[0]["type"], "log")


class LocalServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = web_gui.RuntimeState()
        self.server = web_gui.DeepCodexHTTPServer(self.state, "test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(
        self, method: str, path: str, *, token: str | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=2)
        headers = {}
        if token is not None:
            headers["X-DeepCodex-Token"] = token
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        result_headers = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        return response.status, body, result_headers

    def test_api_rejects_requests_without_ephemeral_token(self) -> None:
        status, body, _headers = self.request("GET", "/api/status")

        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {"error": "Forbidden"})

    def test_server_rejects_dns_rebinding_host_header(self) -> None:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=2)
        connection.request(
            "GET",
            "/",
            headers={"Host": "attacker.example"},
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()

        self.assertEqual(response.status, 421)
        self.assertEqual(json.loads(body), {"error": "Misdirected request"})

    def test_status_accepts_token_and_does_not_return_a_key(self) -> None:
        with mock.patch.object(web_gui, "key_is_available", return_value=True):
            status, body, _headers = self.request(
                "GET", "/api/status", token="test-token"
            )

        payload = json.loads(body)
        self.assertEqual(status, 200)
        self.assertTrue(payload["keyAvailable"])
        self.assertNotIn("apiKey", payload)

    def test_static_page_has_csp_and_embeds_token(self) -> None:
        status, body, headers = self.request("GET", "/")

        self.assertEqual(status, 200)
        self.assertIn("content-security-policy", headers)
        self.assertIn(b"test-token", body)
        self.assertNotIn(b"__DEEPCODEX_TOKEN__", body)


if __name__ == "__main__":
    unittest.main()
