#!/usr/bin/env python3
"""Small, dependency-free client for the experimental Codex App Server.

All protocol-specific method names and message shapes live in this module so
the GUI remains insulated from App Server protocol changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import threading
from typing import Callable

import delegate
import setup


MODEL = "deepseek-v4-flash"
PROVIDER = "deepseek"
APP_NAME = "DeepCodex Preview"
APP_VERSION = "0.1.0"


class AppServerError(RuntimeError):
    """Raised when App Server startup or a JSON-RPC request fails."""


def default_gui_home() -> Path:
    """Return state storage isolated from the user's normal Codex account."""
    configured = os.environ.get("DEEPCODEX_HOME", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".deepcodex"


def find_codex() -> str | None:
    """Find Codex from PATH or the standard ChatGPT application bundle."""
    found = shutil.which("codex")
    if found:
        return found
    if platform.system() == "Darwin":
        bundled = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
        if bundled.is_file():
            return str(bundled)
    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        candidates = (
            Path(local_app_data) / "Programs" / "ChatGPT" / "resources" / "codex.exe",
            Path(local_app_data) / "OpenAI" / "ChatGPT" / "resources" / "codex.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def gui_config_text() -> str:
    """Build a DeepSeek-only config without embedding the API credential."""
    base = setup.profile_text()
    privacy_defaults = """web_search = "disabled"
disable_response_storage = true
"""
    base = base.replace(
        "model_reasoning_effort = \"high\"\n",
        'model_reasoning_effort = "high"\n' + privacy_defaults,
        1,
    )
    return base + """

[analytics]
enabled = false

[features]
plugins = false
remote_plugin = false
"""


def install_gui_config(home: Path | None = None) -> Path:
    """Atomically install the isolated GUI config and return its path."""
    target_home = (home or default_gui_home()).expanduser().resolve()
    target_home.mkdir(parents=True, exist_ok=True)
    target = target_home / "config.toml"
    desired = gui_config_text()
    if not target.exists() or target.read_text(encoding="utf-8") != desired:
        temporary = target_home / ".config.toml.tmp"
        temporary.write_text(desired, encoding="utf-8")
        os.replace(temporary, target)
    try:
        target.chmod(0o600)
    except OSError:
        # Windows ACLs, rather than POSIX modes, protect this file. It contains
        # no credential, so failure to apply a POSIX mode is not fatal there.
        pass
    return target


@dataclass
class PendingRequest:
    event: threading.Event = field(default_factory=threading.Event)
    result: object | None = None
    error: object | None = None


EventHandler = Callable[[dict[str, object]], None]


class AppServerClient:
    """Line-delimited JSON client for ``codex app-server --stdio``."""

    def __init__(
        self,
        *,
        event_handler: EventHandler | None = None,
        codex: str | None = None,
        home: Path | None = None,
        use_curl_bridge: bool | None = None,
    ) -> None:
        self.event_handler = event_handler or (lambda _event: None)
        self.codex = codex or find_codex()
        self.home = (home or default_gui_home()).expanduser().resolve()
        self.use_curl_bridge = (
            platform.system() == "Darwin"
            if use_curl_bridge is None
            else use_curl_bridge
        )
        self.process: subprocess.Popen[str] | None = None
        self.bridge_process: subprocess.Popen[str] | None = None
        self.thread_id: str | None = None
        self.turn_id: str | None = None
        self._next_id = 1
        self._pending: dict[int, PendingRequest] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._closed = threading.Event()

    def start(self, timeout: float = 20.0) -> dict[str, object]:
        if not self.codex:
            raise AppServerError(
                "Codex executable not found. Install Codex or the ChatGPT desktop app first."
            )
        install_gui_config(self.home)
        command = [
            self.codex,
            "app-server",
            "--stdio",
            "--disable",
            "plugins",
            "--disable",
            "remote_plugin",
        ]
        if self.use_curl_bridge:
            self.bridge_process, bridge_url = delegate.start_curl_bridge(
                Path(__file__).resolve().parent,
                stderr=subprocess.DEVNULL,
            )
            command.extend(
                ("-c", f'model_providers.deepseek.base_url="{bridge_url}"')
            )
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(self.home)
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                **delegate.popen_kwargs(),
            )
        except Exception:
            if self.bridge_process is not None:
                delegate.terminate_process_tree(self.bridge_process)
                self.bridge_process = None
            raise
        assert self.process.stdout is not None
        assert self.process.stderr is not None
        threading.Thread(
            target=self._read_stdout,
            name="deepcodex-app-server-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            name="deepcodex-app-server-stderr",
            daemon=True,
        ).start()
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "deepcodex-preview",
                    "title": APP_NAME,
                    "version": APP_VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
            timeout=timeout,
        )
        self.notify("initialized", {})
        return self._require_object(result, "initialize")

    def start_thread(self, cwd: Path, timeout: float = 30.0) -> str:
        workspace = cwd.expanduser().resolve()
        if not workspace.is_dir():
            raise AppServerError(f"Workspace does not exist: {workspace}")
        result = self._require_object(
            self.request(
                "thread/start",
                {
                    "model": MODEL,
                    "modelProvider": PROVIDER,
                    "cwd": str(workspace),
                    "runtimeWorkspaceRoots": [str(workspace)],
                    "approvalPolicy": "on-request",
                    "approvalsReviewer": "user",
                    "sandbox": "workspace-write",
                    "ephemeral": False,
                },
                timeout=timeout,
            ),
            "thread/start",
        )
        thread = self._require_object(result.get("thread"), "thread/start.thread")
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            raise AppServerError("thread/start returned no thread id")
        self.thread_id = thread_id
        return thread_id

    def start_turn(self, text: str, timeout: float = 30.0) -> str:
        prompt = text.strip()
        if not prompt:
            raise AppServerError("Message cannot be empty")
        if not self.thread_id:
            raise AppServerError("No workspace thread has been started")
        result = self._require_object(
            self.request(
                "turn/start",
                {
                    "threadId": self.thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
                timeout=timeout,
            ),
            "turn/start",
        )
        turn = self._require_object(result.get("turn"), "turn/start.turn")
        turn_id = turn.get("id")
        if not isinstance(turn_id, str) or not turn_id:
            raise AppServerError("turn/start returned no turn id")
        self.turn_id = turn_id
        return turn_id

    def interrupt(self, timeout: float = 10.0) -> None:
        if not self.thread_id or not self.turn_id:
            return
        self.request(
            "turn/interrupt",
            {"threadId": self.thread_id, "turnId": self.turn_id},
            timeout=timeout,
        )

    def request(
        self,
        method: str,
        params: dict[str, object],
        *,
        timeout: float = 30.0,
    ) -> object | None:
        request_id = self._allocate_id()
        pending = PendingRequest()
        with self._pending_lock:
            self._pending[request_id] = pending
        try:
            self._send({"id": request_id, "method": method, "params": params})
            if not pending.event.wait(timeout):
                raise AppServerError(f"App Server timed out during {method}")
            if pending.error is not None:
                raise AppServerError(f"App Server rejected {method}: {pending.error}")
            return pending.result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict[str, object]) -> None:
        message: dict[str, object] = {"method": method}
        if params:
            message["params"] = params
        self._send(message)

    def respond(self, request_id: int | str, result: dict[str, object]) -> None:
        self._send({"id": request_id, "result": result})

    def respond_error(
        self,
        request_id: int | str,
        message: str,
        *,
        code: int = -32601,
    ) -> None:
        """Reject an unsupported server-to-client request as JSON-RPC error."""
        self._send(
            {
                "id": request_id,
                "error": {"code": code, "message": message},
            }
        )

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        process = self.process
        if process is not None:
            delegate.terminate_process_tree(process)
            self.process = None
        if self.bridge_process is not None:
            delegate.terminate_process_tree(self.bridge_process)
            self.bridge_process = None
        with self._pending_lock:
            for pending in self._pending.values():
                pending.error = "App Server stopped"
                pending.event.set()

    def handle_message(self, message: dict[str, object]) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is not None and isinstance(method, str):
            self.event_handler({"type": "server_request", "message": message})
            return
        if request_id is not None:
            try:
                numeric_id = int(request_id)
            except (TypeError, ValueError):
                return
            with self._pending_lock:
                pending = self._pending.get(numeric_id)
            if pending is not None:
                pending.result = message.get("result")
                pending.error = message.get("error")
                pending.event.set()
            return
        if isinstance(method, str):
            self.event_handler({"type": "notification", "message": message})

    def _read_stdout(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            for line in self.process.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    self.event_handler({"type": "log", "message": stripped})
                    continue
                if isinstance(message, dict):
                    self.handle_message(message)
        finally:
            if not self._closed.is_set():
                return_code = self.process.poll()
                self.event_handler(
                    {
                        "type": "closed",
                        "message": f"App Server stopped (status {return_code}).",
                    }
                )

    def _read_stderr(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        for line in self.process.stderr:
            text = line.strip()
            if text:
                self.event_handler({"type": "log", "message": text})

    def _send(self, message: dict[str, object]) -> None:
        if self._closed.is_set():
            raise AppServerError("App Server is closed")
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise AppServerError("App Server is not running")
        encoded = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            try:
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise AppServerError("Could not write to App Server") from error

    def _allocate_id(self) -> int:
        with self._pending_lock:
            request_id = self._next_id
            self._next_id += 1
        return request_id

    @staticmethod
    def _require_object(value: object, context: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise AppServerError(f"{context} returned an invalid response")
        return value


__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "AppServerClient",
    "AppServerError",
    "default_gui_home",
    "find_codex",
    "gui_config_text",
    "install_gui_config",
]
