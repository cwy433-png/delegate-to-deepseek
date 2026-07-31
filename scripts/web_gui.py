#!/usr/bin/env python3
"""Local browser GUI for Codex powered exclusively by DeepSeek V4 Flash."""

from __future__ import annotations

from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import threading
from typing import Any
from urllib.parse import parse_qs, urlsplit
import webbrowser

from app_server import APP_NAME, APP_VERSION, AppServerClient
import deepseek_key
import setup


MAX_BODY = 1024 * 1024
WEB_ROOT = Path(__file__).resolve().parent.parent / "web"


def key_is_available() -> bool:
    return bool(
        os.environ.get("DEEPSEEK_API_KEY", "").strip()
        or deepseek_key.native_key()
    )


def save_key(key: str) -> None:
    error = setup.validate_key(key)
    if error:
        raise ValueError(error)
    system = platform.system()
    if system == "Darwin":
        status = setup.save_key_to_keychain(key)
    elif system == "Windows":
        status = setup.save_key_windows(key)
    else:
        raise RuntimeError(
            "Linux 预览版请在启动 DeepCodex 前设置 DEEPSEEK_API_KEY。"
        )
    if status != 0:
        raise RuntimeError("无法写入系统密钥库。")


def pick_workspace(initial: str) -> str | None:
    """Open a native folder chooser where the platform provides one."""
    system = platform.system()
    if system == "Darwin":
        script = 'POSIX path of (choose folder with prompt "选择 Codex 项目文件夹")'
        result = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout.strip().rstrip("/") or "/"
        return None
    if system == "Windows":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            return None
        script = r'''
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = 'Select a project folder for Codex'
$dialog.UseDescriptionForTitle = $true
if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {
  [Console]::Out.Write($dialog.SelectedPath)
  exit 0
}
exit 130
'''.strip()
        result = subprocess.run(
            [powershell, "-NoProfile", "-STA", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
        return None
    return str(Path(initial).expanduser()) if initial else str(Path.home())


class RuntimeState:
    def __init__(self) -> None:
        self.client: AppServerClient | None = None
        self.connected = False
        self.busy = False
        self.workspace = str(Path.home())
        self.platform = platform.system().lower()
        self.thread_id: str | None = None
        self._events: deque[dict[str, object]] = deque(maxlen=2000)
        self._sequence = 0
        self._lock = threading.Lock()

    def emit(self, event_type: str, **payload: object) -> None:
        with self._lock:
            self._sequence += 1
            self._events.append(
                {"seq": self._sequence, "type": event_type, **payload}
            )

    def events_after(self, sequence: int) -> list[dict[str, object]]:
        with self._lock:
            return [dict(event) for event in self._events if event["seq"] > sequence]

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "version": APP_VERSION,
                "keyAvailable": key_is_available(),
                "connected": self.connected,
                "busy": self.busy,
                "workspace": self.workspace,
                "platform": self.platform,
                "threadId": self.thread_id,
                "lastSeq": self._sequence,
            }

    def on_client_event(self, event: dict[str, object]) -> None:
        event_type = event.get("type")
        if event_type == "notification":
            message = event.get("message")
            if isinstance(message, dict):
                method = message.get("method")
                params = message.get("params")
                if method == "turn/completed":
                    with self._lock:
                        self.busy = False
                self.emit("notification", method=method, params=params or {})
        elif event_type == "server_request":
            message = event.get("message")
            if isinstance(message, dict):
                method = message.get("method")
                request_id = message.get("id")
                if method not in (
                    "item/commandExecution/requestApproval",
                    "item/fileChange/requestApproval",
                ):
                    client = self.client
                    if client is not None and isinstance(request_id, (int, str)):
                        client.respond_error(
                            request_id,
                            f"{APP_NAME} does not support server request {method}",
                        )
                    self.emit("log", message=f"unsupported server request rejected: {method}")
                    return
                self.emit(
                    "server_request",
                    requestId=request_id,
                    method=method,
                    params=message.get("params") or {},
                )
        elif event_type == "log":
            self.emit("log", message=event.get("message", ""))
        elif event_type == "closed":
            with self._lock:
                self.connected = False
                self.busy = False
            self.emit("closed", message=event.get("message", "App Server stopped"))

    def connect(self, workspace: str) -> None:
        path = Path(workspace).expanduser().resolve()
        if not path.is_dir():
            raise ValueError("请选择有效的项目文件夹。")
        with self._lock:
            if self.busy:
                raise RuntimeError("当前正在执行其他操作。")
            self.busy = True
            self.workspace = str(path)
        self.emit("status", message="正在启动 Codex App Server…")
        threading.Thread(
            target=self._connect_worker,
            args=(path,),
            name="deepcodex-web-connect",
            daemon=True,
        ).start()

    def _connect_worker(self, workspace: Path) -> None:
        old_client = self.client
        if old_client is not None:
            old_client.close()
        client = AppServerClient(event_handler=self.on_client_event)
        self.client = client
        try:
            info = client.start()
            thread_id = client.start_thread(workspace)
        except Exception as error:
            client.close()
            with self._lock:
                self.connected = False
                self.busy = False
            self.emit("fatal", message=str(error))
            return
        with self._lock:
            self.connected = True
            self.busy = False
            self.thread_id = thread_id
            self.platform = str(info.get("platformOs", self.platform))
        self.emit(
            "connected",
            threadId=thread_id,
            workspace=str(workspace),
            platform=self.platform,
        )

    def start_turn(self, text: str) -> None:
        prompt = text.strip()
        if not prompt:
            raise ValueError("消息不能为空。")
        with self._lock:
            if not self.connected or self.client is None:
                raise RuntimeError("请先连接项目。")
            if self.busy:
                raise RuntimeError("上一轮仍在进行中。")
            self.busy = True
        self.emit("user_message", text=prompt)
        threading.Thread(
            target=self._turn_worker,
            args=(prompt,),
            name="deepcodex-web-turn",
            daemon=True,
        ).start()

    def _turn_worker(self, prompt: str) -> None:
        assert self.client is not None
        try:
            turn_id = self.client.start_turn(prompt)
            self.emit("turn_started", turnId=turn_id)
        except Exception as error:
            with self._lock:
                self.busy = False
            self.emit("turn_error", message=str(error))

    def interrupt(self) -> None:
        client = self.client
        if client is None:
            return
        threading.Thread(target=self._interrupt_worker, daemon=True).start()

    def _interrupt_worker(self) -> None:
        assert self.client is not None
        try:
            self.client.interrupt()
        except Exception as error:
            self.emit("turn_error", message=str(error))

    def respond(self, request_id: int | str, decision: str) -> None:
        if decision not in ("accept", "decline", "cancel", "acceptForSession"):
            raise ValueError("无效的审批决定。")
        client = self.client
        if client is None:
            raise RuntimeError("App Server 未连接。")
        client.respond(request_id, {"decision": decision})
        self.emit("approval_resolved", requestId=request_id, decision=decision)

    def close(self) -> None:
        if self.client is not None:
            self.client.close()


class DeepCodexHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state: RuntimeState, token: str) -> None:
        super().__init__(("127.0.0.1", 0), DeepCodexHandler)
        self.state = state
        self.token = token


class DeepCodexHandler(BaseHTTPRequestHandler):
    server: DeepCodexHTTPServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if not self._require_local_host():
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/api/status":
            if not self._require_token():
                return
            self._json(200, self.server.state.status())
            return
        if parsed.path == "/api/events":
            if not self._require_token():
                return
            raw = parse_qs(parsed.query).get("after", ["0"])[0]
            try:
                after = int(raw)
            except ValueError:
                after = 0
            self._json(200, {"events": self.server.state.events_after(after)})
            return
        if parsed.path == "/":
            self._static(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        static = {
            "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
            "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
        }.get(parsed.path)
        if static:
            self._static(*static)
            return
        self._json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if not self._require_local_host():
            return
        if not self._require_token():
            return
        try:
            data = self._read_json()
            if self.path == "/api/key":
                key = data.get("apiKey")
                if not isinstance(key, str):
                    raise ValueError("API Key 不能为空。")
                try:
                    save_key(key.strip())
                finally:
                    key = ""
                    data.clear()
                self._json(200, {"ok": True})
            elif self.path == "/api/pick-workspace":
                picked = pick_workspace(str(data.get("initial", "")))
                self._json(200, {"path": picked})
            elif self.path == "/api/connect":
                self.server.state.connect(str(data.get("workspace", "")))
                self._json(202, {"ok": True})
            elif self.path == "/api/turn":
                self.server.state.start_turn(str(data.get("text", "")))
                self._json(202, {"ok": True})
            elif self.path == "/api/interrupt":
                self.server.state.interrupt()
                self._json(202, {"ok": True})
            elif self.path == "/api/approval":
                request_id = data.get("requestId")
                if not isinstance(request_id, (int, str)):
                    raise ValueError("审批请求缺少 id。")
                self.server.state.respond(request_id, str(data.get("decision", "")))
                self._json(200, {"ok": True})
            elif self.path == "/api/shutdown":
                self._json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json(404, {"error": "Not found"})
        except (ValueError, RuntimeError) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            self.server.state.emit("fatal", message=str(error))
            self._json(500, {"error": str(error)})

    def _require_token(self) -> bool:
        if self.headers.get("X-DeepCodex-Token") != self.server.token:
            self._json(403, {"error": "Forbidden"})
            return False
        return True

    def _require_local_host(self) -> bool:
        """Reject DNS-rebinding requests whose Host is not this loopback server."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed:
            self._json(421, {"error": "Misdirected request"})
            return False
        return True

    def _read_json(self) -> dict[str, Any]:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("Invalid Content-Length") from error
        if size < 0 or size > MAX_BODY:
            raise ValueError("Request body is too large")
        raw = self.rfile.read(size)
        value = json.loads(raw or b"{}")
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object")
        return value

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self._json(404, {"error": "Static asset missing"})
            return
        body = path.read_bytes()
        if path.name == "index.html":
            body = body.replace(b"__DEEPCODEX_TOKEN__", self.server.token.encode("ascii"))
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    state = RuntimeState()
    token = secrets.token_urlsafe(32)
    server = DeepCodexHTTPServer(state, token)
    host, port = server.server_address
    url = f"http://{host}:{port}/"
    print(f"{APP_NAME} is running at {url}")
    print("Close it from the GUI or press Ctrl+C here to stop.")
    threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        state.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
