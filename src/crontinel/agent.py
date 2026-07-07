"""
Crontinel Agent — remote command execution daemon.

Polls app.crontinel.com via HTTP for pending commands, executes them,
and reports results. Uses HTTP polling (registration -> poll -> execute -> report)
instead of SSE for simplicity and portability.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import ssl
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Callable

log = logging.getLogger(__name__)

REGISTER_PATH = "/v1/agents/register"
COMMANDS_PATH = "/v1/agents/{app_id}/commands"
RESULT_PATH = "/v1/ingest/agent-result"
HEARTBEAT_PATH = "/v1/agents/heartbeat"

POLL_INTERVAL_S = 5
HEARTBEAT_INTERVAL_S = 60
MAX_RECONNECT_DELAY_S = 60
HTTP_TIMEOUT_S = 15
MAX_COMMAND_OUTPUT = 1_048_576  # 1 MB max output per command
DEFAULT_API_URL = "https://app.crontinel.com"


class Agent:
    """
    Crontinel agent daemon.

    Registers with the cloud, then polls for pending commands via HTTP,
    executes them as subprocesses, and reports results back.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        app_id: str | None = None,
        output_writer: Callable[[str], None] | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("CRONTINEL_API_KEY", "")
        if not self.api_key:
            raise ValueError(
                "CRONTINEL_API_KEY is not set. Set it in your environment "
                "or pass it to the constructor."
            )
        self.app_id = app_id or os.environ.get("CRONTINEL_APP_ID", "")
        if not self.app_id:
            raise ValueError(
                "CRONTINEL_APP_ID is not set. Set it in your environment "
                "or pass it to the constructor."
            )

        base_url = (api_url or os.environ.get("CRONTINEL_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.api_url = base_url.rstrip("/api")  # strip trailing /api if present
        self.output_writer = output_writer or (lambda msg: print(msg))

        self.running = True
        self.last_heartbeat_at = time.time()
        self.started_at = time.time()
        self.reconnect_attempt = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        """Start the agent daemon (blocking loop). Press Ctrl+C to stop."""
        self.started_at = time.time()
        self.last_heartbeat_at = self.started_at
        self._install_signal_handlers()

        self._log(f"Agent starting...")
        self._log(f"Agent ID: {self._agent_id()}")
        self._log(f"App ID: {self.app_id}")
        self._log(f"API URL: {self.api_url}")

        # Register with the cloud on startup
        self.register()

        self._log("Agent registered. Starting poll loop...")

        while self.running:
            try:
                self._poll_once()
                self.reconnect_attempt = 0
            except Exception as exc:
                log.error("Poll error: %s", exc)
                self._log(f"Poll error: {exc}")

            if not self.running:
                break

            # Throttle loop
            self._sleep(POLL_INTERVAL_S)

        self._log("Agent stopped.")

    def is_configured(self) -> bool:
        """Whether the agent has an API key and app ID configured."""
        return bool(self.api_key) and bool(self.app_id)

    # ── Registration ───────────────────────────────────────────────────────────

    def register(self) -> None:
        """Register this agent with the Crontinel cloud."""
        try:
            body = {
                "agent_id": self._agent_id(),
                "agent_name": self._agent_name(),
                "metadata": {
                    "python_version": os.environ.get("PYTHON_VERSION", ""),
                    "platform": os.name,
                },
            }
            result = self._http_post(self.api_url + REGISTER_PATH, json_body=body)
            if result is not None:
                self._log(f"Registered: id={result.get('id', 'unknown')}")
            else:
                self._log("Registration response was empty (continuing anyway)")
        except Exception as exc:
            self._log(f"Registration error: {exc} (continuing anyway)")

    # ── Poll Loop ──────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        """Single poll iteration: heartbeat check + fetch + execute."""
        self._check_heartbeat()

        commands = self._fetch_commands()

        if not commands:
            return

        self._log(f"Received {len(commands)} command(s)")

        for cmd in commands:
            if not self.running:
                break
            self._execute_and_report(cmd)

    def _fetch_commands(self) -> list[dict[str, Any]]:
        """Poll the cloud for pending commands."""
        url = f"{self.api_url}{COMMANDS_PATH.format(app_id=self.app_id)}"
        try:
            data = self._http_get(url)
            if data is None:
                return []
            return data.get("data", [])
        except Exception as exc:
            self._log(f"Commands fetch error: {exc}")
            return []

    # ── Execute + Report ───────────────────────────────────────────────────────

    def _execute_and_report(self, cmd: dict[str, Any]) -> None:
        """Execute a command and report the result."""
        trigger_id = str(cmd["id"])
        command_str = cmd["command"]

        self._log(f"Executing [{trigger_id}]: {command_str}")

        started_at = datetime.now(timezone.utc)
        start_ms = int(time.time() * 1000)

        exit_code = 0
        output = ""
        status = "completed"
        error_message: str | None = None

        # Acknowledge receipt first
        self._report_result(trigger_id=trigger_id, status="acknowledged")

        env_overrides = cmd.get("payload") or {}
        cmd_timeout = 300

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
                self._log(f"Command [{trigger_id}] output truncated to {MAX_COMMAND_OUTPUT} bytes")
            status = "completed" if exit_code == 0 else "failed"
        except subprocess.TimeoutExpired:
            exit_code = -1
            output = f"Command timed out after {cmd_timeout}s"
            status = "failed"
            error_message = f"Timed out after {cmd_timeout}s"
            self._log(f"Command [{trigger_id}] timed out after {cmd_timeout}s")
        except Exception as exc:
            exit_code = -1
            output = str(exc)
            status = "failed"
            error_message = str(exc)
            self._log(f"Command [{trigger_id}] error: {exc}")

        finished_at = datetime.now(timezone.utc)
        duration_ms = int(time.time() * 1000) - start_ms

        self._log(
            f"[{trigger_id}] {status} ({duration_ms}ms, exit {exit_code})"
        )

        self._report_result(
            trigger_id=trigger_id,
            status=status,
            exit_code=exit_code,
            output=output,
            error_message=error_message,
            started_at=started_at.isoformat(),
            finished_at=finished_at.isoformat(),
            duration_ms=duration_ms,
        )

    def _report_result(
        self,
        trigger_id: str,
        status: str,
        exit_code: int | None = None,
        output: str | None = None,
        error_message: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Report a command execution result to the cloud."""
        payload: dict[str, Any] = {
            "trigger_id": trigger_id,
            "status": status,
        }
        if exit_code is not None:
            payload["exit_code"] = exit_code
        if output is not None:
            payload["output"] = output
        if error_message is not None:
            payload["error_message"] = error_message
        if started_at is not None:
            payload["started_at"] = started_at
        if finished_at is not None:
            payload["finished_at"] = finished_at
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms

        try:
            self._http_post(self.api_url + RESULT_PATH, json_body=payload)
        except Exception as exc:
            self._log(f"Result report error: {exc}")

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    def _check_heartbeat(self) -> None:
        """Send a heartbeat if it's due."""
        if (time.time() - self.last_heartbeat_at) >= HEARTBEAT_INTERVAL_S:
            self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        """POST a heartbeat to the cloud."""
        self.last_heartbeat_at = time.time()
        uptime = int(time.time() - self.started_at)

        url = f"{self.api_url}{HEARTBEAT_PATH}"
        try:
            self._http_post(
                url,
                json_body={
                    "agent_id": self._agent_id(),
                    "metadata": {
                        "uptime_seconds": uptime,
                    },
                },
            )
            self._log(f"Heartbeat sent (uptime: {uptime}s)")
        except Exception as exc:
            self._log(f"Heartbeat error: {exc}")

    # ── HTTP Helpers ───────────────────────────────────────────────────────────

    def _http_get(self, url: str) -> dict[str, Any] | None:
        """Low-level HTTP GET with auth."""
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return None
        scheme = parsed.scheme or "https"
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"Authorization: Bearer {self.api_key}\r\n"
            f"Accept: application/json\r\n"
            f"Connection: close\r\n"
            f"User-Agent: crontinel-agent/1.0\r\n"
            f"\r\n"
        )

        try:
            sock = socket.create_connection((host, port), timeout=HTTP_TIMEOUT_S)
        except OSError as exc:
            self._log(f"HTTP GET connect failed: {exc}")
            return None

        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                sock = ctx.wrap_socket(sock, server_hostname=host)

            sock.sendall(request.encode())

            response = b""
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                response += chunk
        except (ssl.SSLError, OSError) as exc:
            self._log(f"HTTP GET error: {exc}")
            return None
        finally:
            sock.close()

        # Parse response
        header_end = response.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        status_line = response[: response.find(b"\r\n")].decode()
        body = response[header_end + 4 :]

        parts = status_line.split(None, 2)
        if len(parts) >= 2 and parts[1].startswith("2"):
            if body:
                try:
                    return json.loads(body.decode())
                except json.JSONDecodeError:
                    pass
        return None

    def _http_post(
        self, url: str, json_body: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Low-level HTTP POST with JSON body over a fresh socket."""
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname
        if not host:
            return None
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
            sock = socket.create_connection((host, port), timeout=HTTP_TIMEOUT_S)
        except OSError:
            return None

        try:
            if scheme == "https":
                ctx = ssl.create_default_context()
                ctx.check_hostname = True
                ctx.verify_mode = ssl.CERT_REQUIRED
                sock = ctx.wrap_socket(sock, server_hostname=host)

            sock.sendall(request)

            response = b""
            while True:
                chunk = sock.recv(8192)
                if not chunk:
                    break
                response += chunk
        except (ssl.SSLError, OSError) as exc:
            log.warning("HTTP POST %s error: %s", path, exc)
            return None
        finally:
            sock.close()

        # Parse response
        header_end = response.find(b"\r\n\r\n")
        if header_end == -1:
            return None

        status_line = response[: response.find(b"\r\n")].decode()
        body_data = response[header_end + 4 :]

        parts = status_line.split(None, 2)
        if len(parts) >= 2 and parts[1].startswith("2"):
            if body_data:
                try:
                    return json.loads(body_data.decode())
                except json.JSONDecodeError:
                    pass
        return None

    # ── Signal Handling ────────────────────────────────────────────────────────

    def _install_signal_handlers(self) -> None:
        """Install graceful-shutdown signal handlers."""

        def _shutdown(signum: int, _frame) -> None:  # type: ignore[type-arg]
            self._log(f"Received signal {signum}. Shutting down gracefully...")
            self.running = False

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _agent_id(self) -> str:
        return socket.gethostname()

    def _agent_name(self) -> str:
        return f"python-agent:{self._agent_id()}"

    def _sleep(self, seconds: int) -> None:
        for _ in range(seconds):
            if not self.running:
                break
            time.sleep(1)

    # ── Logging ────────────────────────────────────────────────────────────────

    def _log(self, message: str) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.output_writer(f"[{ts}] Crontinel Agent: {message}")
