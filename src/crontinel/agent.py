"""
Crontinel Agent — remote command execution daemon.

Connects to app.crontinel.com via SSE to receive and execute commands,
reports results via HTTP POST, sends heartbeats every 60s, and
automatically reconnects with exponential backoff on disconnect.
"""

from __future__ import annotations

import json
import logging
import os
import select
import signal
import socket
import ssl
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

SSE_PATH = "/api/v1/agent/stream"
COMMAND_RESULT_PATH = "/api/v1/agent/command/{command_id}/result"
HEARTBEAT_PATH = "/api/v1/agent/heartbeat"
HEARTBEAT_INTERVAL = 60
MAX_RECONNECT_DELAY = 60
STREAM_SELECT_TIMEOUT = 1  # seconds
MAX_COMMAND_OUTPUT = 1_048_576  # 1 MB max output per command
DEFAULT_API_URL = "https://app.crontinel.com"


class SseEvent:
    """A parsed Server-Sent Event (event type, data, optional id)."""

    def __init__(self) -> None:
        self.event: str = ""
        self.data: str = ""
        self.id: str = ""


class Agent:
    """
    Crontinel agent daemon.

    Connects to the SaaS SSE stream, listens for ``command`` events,
    executes them as subprocesses, and reports results back.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        output_writer: Callable[[str], None] | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("CRONTINEL_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "CRONTINEL_API_KEY is not set. Set it in your environment "
                "or pass it to the constructor."
            )
        base_url = (api_url or os.environ.get("CRONTINEL_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.api_url = base_url.rstrip("/api")  # strip trailing /api if present
        self.output_writer = output_writer or (lambda msg: print(msg))

        self.running = True
        self.sse_buffer = ""
        self.current_event: SseEvent | None = None
        self.last_heartbeat_at = 0.0
        self.started_at = 0.0
        self.reconnect_attempt = 0
        self._sse_readahead = ""

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the agent daemon (blocking loop). Press Ctrl+C to stop."""
        self.started_at = time.time()
        self.last_heartbeat_at = self.started_at
        self._install_signal_handlers()
        self._log("Agent starting...")

        while self.running:
            try:
                self._connect_and_listen()
            except Exception as exc:
                log.error("Connection error: %s", exc)
                self._log(f"Connection error: {exc}")

            if not self.running:
                break

            self.reconnect_attempt += 1
            delay = min(
                (1 << min(self.reconnect_attempt - 1, 5))
                if self.reconnect_attempt > 1
                else 1,
                MAX_RECONNECT_DELAY,
            )
            self._log(
                f"Reconnecting in {delay}s (attempt {self.reconnect_attempt})..."
            )
            time.sleep(delay)

        self._log("Agent stopped.")

    def is_configured(self) -> bool:
        """Whether the agent has an API key configured."""
        return bool(self.api_key)

    def feed_sse_data(self, data: str) -> None:
        """
        Feed raw SSE data into the parser (useful for testing).

        Processes line-by-line and dispatches complete events.
        """
        self.sse_buffer += data
        while "\n" in self.sse_buffer:
            line, self.sse_buffer = self.sse_buffer.split("\n", 1)
            self._feed_line(line.rstrip("\r"))

    def handle_command_event(self, json_data: str) -> None:
        """
        Parse and execute a command event payload.
        Public so tests can call it directly without going through SSE.
        """
        try:
            payload = json.loads(json_data)
        except json.JSONDecodeError:
            log.warning("Malformed command event: %s", json_data)
            self._log("Malformed command event received.")
            return

        command_id = payload.get("command_id")
        command_str = payload.get("command")
        if not command_id or not command_str:
            log.warning(
                "Malformed command event: missing command_id or command field"
            )
            self._log(
                "Malformed command event received (missing required fields)."
            )
            return

        env_overrides = payload.get("env", {})
        cmd_timeout = payload.get("timeout", 300)

        self._log(f"Executing command [{command_id}]: {command_str}")

        started_at = datetime.now(timezone.utc)
        start_micro = time.time()

        env = None
        if env_overrides:
            env = {**os.environ, **env_overrides}

        try:
            result = subprocess.run(
                command_str,
                shell=True,
                capture_output=True,
                text=True,
                timeout=cmd_timeout,
                env=env,
            )
            exit_code = result.returncode
            output = result.stdout + result.stderr
            if len(output) > MAX_COMMAND_OUTPUT:
                output = output[:MAX_COMMAND_OUTPUT]
                self._log(f"Command [{command_id}] output truncated to {MAX_COMMAND_OUTPUT} bytes")
            status = "completed" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            exit_code = -1
            output = f"Command timed out after {cmd_timeout}s"
            status = "failed"
            self._log(f"Command [{command_id}] timed out after {cmd_timeout}s")
        except Exception as exc:
            exit_code = -1
            output = str(exc)
            status = "failed"
            self._log(f"Command [{command_id}] error: {exc}")

        finished_at = datetime.now(timezone.utc)
        duration_ms = int((time.time() - start_micro) * 1000)

        self._log(
            f"Command [{command_id}] finished: {status} "
            f"({duration_ms}ms, exit {exit_code})"
        )

        self._report_command_result(
            command_id=command_id,
            status=status,
            exit_code=exit_code,
            output=output,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_ms=duration_ms,
        )

    # ── Connect / Listen ───────────────────────────────────────────────────────

    def _connect_and_listen(self) -> None:
        """Open a raw HTTPS connection to the SSE endpoint and read the stream."""
        self._reset_sse_parser()
        self.reconnect_attempt = 0

        url = f"{self.api_url}{SSE_PATH}"
        self._log(f"Connecting to {url}...")

        sock = self._open_sse_connection(url)
        if sock is None:
            return

        if not self._read_response_headers(sock):
            sock.close()
            return

        self._log("Connected. Listening for commands...")

        try:
            self._read_sse_stream(sock)
        finally:
            sock.close()

        self._log("Disconnected.")

    def _open_sse_connection(self, url: str) -> socket.socket | None:
        """
        Open a TCP/TLS socket to the SSE endpoint and send the HTTP request.

        Returns the connected socket, or ``None`` on failure.
        """
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            log.error("Crontinel Agent: invalid SSE URL — no hostname")
            return None

        scheme = parsed.scheme or "https"
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        try:
            sock = socket.create_connection((host, port), timeout=30)
        except OSError as exc:
            log.error("Crontinel Agent: socket connect failed: %s", exc)
            self._log(f"Socket connect failed: {exc}")
            return None

        if scheme == "https":
            ctx = ssl.create_default_context()
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            try:
                sock = ctx.wrap_socket(sock, server_hostname=host)
            except ssl.SSLError as exc:
                log.error("Crontinel Agent: SSL handshake failed: %s", exc)
                self._log(f"SSL handshake failed: {exc}")
                sock.close()
                return None

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Authorization: Bearer {self.api_key}\r\n"
            f"Accept: text/event-stream\r\n"
            f"Cache-Control: no-cache\r\n"
            f"Connection: keep-alive\r\n"
            f"User-Agent: crontinel-agent/1.0\r\n"
            f"\r\n"
        )

        try:
            sock.sendall(request.encode())
        except OSError as exc:
            log.error("Crontinel Agent: failed to send request: %s", exc)
            self._log(f"Failed to send request: {exc}")
            sock.close()
            return None

        return sock

    def _read_response_headers(self, sock: socket.socket) -> bool:
        """
        Read and validate the HTTP response status line and headers.

        Manually reads from the raw socket (no ``makefile`` wrapper)
        so SSE body data is not lost in a BufferedReader buffer.
        Any data after the ``\\r\\n\\r\\n`` header boundary is saved
        in ``self._sse_readahead`` and fed to the parser first.

        Returns ``True`` if the status is 2xx.
        """
        buf = b""
        while b"\r\n\r\n" not in buf:
            try:
                chunk = sock.recv(4096)
            except OSError as exc:
                self._log(f"Connection closed while reading response: {exc}")
                return False
            if not chunk:
                self._log("Connection closed while reading response.")
                return False
            buf += chunk

        # Split at the header/body boundary
        header_bytes, extra = buf.split(b"\r\n\r\n", 1)
        header_text = header_bytes.decode("utf-8", errors="replace")

        # Save any extra data for the SSE parser
        if extra:
            self._sse_readahead = extra.decode("utf-8", errors="replace")

        lines = header_text.split("\r\n")
        if not lines:
            self._log("Empty response headers.")
            return False

        status_line = lines[0].strip()
        parts = status_line.split(None, 2)
        if len(parts) < 2 or not parts[1].startswith("2"):
            self._log(f"Unexpected response: {status_line}")
            return False

        return True

    def _read_sse_stream(self, sock: socket.socket) -> None:
        """
        Non-blocking read loop on the SSE socket.

        Reads available data, feeds the SSE parser, fires heartbeats,
        and dispatches signal handlers.
        """
        sock.setblocking(False)

        # Feed any readahead from the header parsing phase
        if self._sse_readahead:
            self.feed_sse_data(self._sse_readahead)
            self._sse_readahead = ""

        while self.running:
            ready = select.select([sock], [], [], STREAM_SELECT_TIMEOUT)

            if not ready[0]:
                # Timeout — check heartbeats
                self._check_heartbeat()
                continue

            try:
                chunk = sock.recv(8192).decode()
            except (BlockingIOError, ssl.SSLWantReadError):
                self._check_heartbeat()
                continue
            except OSError:
                break

            if not chunk:
                break

            self.feed_sse_data(chunk)
            self._check_heartbeat()

    # ── SSE Parser ─────────────────────────────────────────────────────────────

    def _feed_line(self, line: str) -> None:
        """Process a single SSE line (without trailing \\n)."""
        if line == "":
            self._dispatch_current_event()
            self.current_event = None
            return

        if ":" not in line:
            return

        field, value = line.split(":", 1)
        value = value.lstrip()

        if self.current_event is None:
            self.current_event = SseEvent()

        if field == "event":
            self.current_event.event = value
        elif field == "data":
            self.current_event.data += value
        elif field == "id":
            self.current_event.id = value

    def _dispatch_current_event(self) -> None:
        """Dispatch a complete SSE event."""
        if self.current_event is None:
            return

        ev = self.current_event
        if ev.event == "command" and ev.data:
            self.handle_command_event(ev.data)
        elif ev.event == "ping":
            self._log("Received server ping.")

    def _reset_sse_parser(self) -> None:
        self.sse_buffer = ""
        self.current_event = None
        self._sse_readahead = ""

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    def _check_heartbeat(self) -> None:
        """Send a heartbeat if it's due."""
        if (time.time() - self.last_heartbeat_at) >= HEARTBEAT_INTERVAL:
            self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        """POST a heartbeat to the SaaS."""
        self.last_heartbeat_at = time.time()
        uptime = int(time.time() - self.started_at)

        url = f"{self.api_url}{HEARTBEAT_PATH}"
        try:
            self._http_post(
                url,
                json_body={
                    "status": "connected",
                    "uptime_seconds": uptime,
                },
            )
        except Exception as exc:
            log.warning("Heartbeat exception: %s", exc)

    # ── Command Result Reporting ───────────────────────────────────────────────

    def _report_command_result(self, **kwargs: str | int) -> None:
        """POST a command execution result to the SaaS."""
        command_id = kwargs.get("command_id", "")
        url = f"{self.api_url}{COMMAND_RESULT_PATH.format(command_id=command_id)}"
        try:
            self._http_post(url, json_body=kwargs)
        except Exception as exc:
            log.warning("Exception reporting command result: %s", exc)

    # ── HTTP Helper ────────────────────────────────────────────────────────────

    def _http_post(self, url: str, json_body: dict) -> None:
        """Low-level HTTP POST with JSON body over a fresh socket."""
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return
        scheme = parsed.scheme or "https"
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"

        body = json.dumps(json_body).encode()
        request = (
            f"POST {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Authorization: Bearer {self.api_key}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"User-Agent: crontinel-agent/1.0\r\n"
            f"\r\n"
        ).encode() + body

        try:
            sock = socket.create_connection((host, port), timeout=10)
        except OSError:
            return

        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                sock = ctx.wrap_socket(sock, server_hostname=host)

            sock.sendall(request)
            # Read response (consume headers and body)
            rfile = sock.makefile("rb")
            status_line = rfile.readline().decode().strip()
            status_code = 0
            if status_line:
                parts = status_line.split(None, 2)
                if len(parts) >= 2:
                    try:
                        status_code = int(parts[1])
                    except ValueError:
                        pass
            if status_code and status_code not in (200, 201, 202, 204):
                log.warning(
                    "HTTP POST %s returned %s", path, status_line
                )
            # Drain the rest
            while rfile.readline():
                pass
        except (ssl.SSLError, OSError) as exc:
            log.warning("HTTP POST %s error: %s", path, exc)
        finally:
            sock.close()

    # ── Signal Handling ────────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        """Install graceful-shutdown signal handlers."""

        def _shutdown(signum: int, _frame) -> None:  # type: ignore[type-arg]
            self._log(f"Received signal {signum}. Shutting down gracefully...")
            self.running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

    # ── Logging ────────────────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.output_writer(f"[{ts}] Crontinel Agent: {message}")
