#!/usr/bin/env python3
"""Forward local Codex Responses requests through the system curl TLS stack."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from urllib.parse import urlsplit


UPSTREAM = "https://api.deepseek.com/responses"
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key-command", required=True)
    return parser.parse_args()


def curl_config_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], key_command: Path):
        super().__init__(address, BridgeHandler)
        self.key_command = key_command


class BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: BridgeServer

    def log_message(self, format: str, *args: object) -> None:
        print(f"DeepSeek bridge: {format % args}", file=sys.stderr)

    def send_json_error(self, status: int, message: str) -> None:
        body = json.dumps({"error": {"message": message}}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/responses":
            self.send_json_error(404, "The local bridge only accepts /responses.")
            return
        if self.headers.get("Transfer-Encoding"):
            self.send_json_error(411, "Chunked request bodies are not supported.")
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_json_error(400, "Invalid Content-Length.")
            return
        body = self.rfile.read(content_length)

        key_result = subprocess.run(
            [sys.executable, str(self.server.key_command)],
            check=False,
            capture_output=True,
            text=True,
        )
        key = key_result.stdout.strip()
        if key_result.returncode != 0 or not key:
            self.send_json_error(503, "The DeepSeek credential is unavailable.")
            return

        config_read, config_write = os.pipe()
        process: subprocess.Popen[bytes] | None = None
        try:
            curl = shutil.which("curl")
            if not curl:
                self.send_json_error(503, "The system curl executable is unavailable.")
                return
            process = subprocess.Popen(
                [
                    curl,
                    "--silent",
                    "--show-error",
                    "--no-buffer",
                    "--http1.1",
                    "--dump-header",
                    "-",
                    "--output",
                    "-",
                    "--config",
                    f"/dev/fd/{config_read}",
                    "--request",
                    "POST",
                    "--data-binary",
                    "@-",
                    UPSTREAM,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(config_read,),
                start_new_session=True,
            )
        finally:
            os.close(config_read)

        assert process is not None
        config = (
            'header = "Authorization: Bearer '
            + curl_config_value(key)
            + '"\nheader = "Content-Type: application/json"\n'
            + 'header = "Accept: text/event-stream"\nheader = "Expect:"\n'
        ).encode("utf-8")
        key = ""
        try:
            os.write(config_write, config)
        finally:
            os.close(config_write)
            config = b""

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            process.stdin.write(body)
            process.stdin.close()
            status, response_headers = self.read_response_head(process.stdout)
        except (BrokenPipeError, EOFError, ValueError) as error:
            stderr = process.stderr.read().decode("utf-8", "replace").strip()
            process.wait()
            detail = stderr or str(error)
            self.send_json_error(502, f"DeepSeek upstream connection failed: {detail}")
            return

        self.send_response(status)
        for name, value in response_headers:
            if name.lower() not in HOP_BY_HOP_HEADERS:
                self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        try:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            process.terminate()
        finally:
            process.wait()

    @staticmethod
    def read_response_head(
        stream: object,
    ) -> tuple[int, list[tuple[str, str]]]:
        while True:
            status_line = stream.readline()
            if not status_line:
                raise EOFError("curl returned no HTTP response")
            decoded_status = status_line.decode("iso-8859-1").strip()
            parts = decoded_status.split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                raise ValueError(f"invalid upstream status line: {decoded_status}")
            status = int(parts[1])
            headers: list[tuple[str, str]] = []
            while True:
                line = stream.readline()
                if not line:
                    raise EOFError("curl ended while returning HTTP headers")
                if line in (b"\r\n", b"\n"):
                    break
                name, separator, value = line.decode("iso-8859-1").partition(":")
                if not separator:
                    raise ValueError("invalid upstream HTTP header")
                headers.append((name.strip(), value.strip()))
            if status >= 200:
                return status, headers


def main() -> int:
    args = parse_args()
    server = BridgeServer(("127.0.0.1", 0), Path(args.key_command).resolve())
    host, port = server.server_address
    print(f"http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
