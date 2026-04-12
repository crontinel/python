"""
Crontinel monitoring SDK for Python applications.
Send cron, queue, and job monitoring events from any Python app.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, TypeVar

__version__ = "0.1.0"

DEFAULT_API_URL = "https://app.crontinel.com"


class Crontinel:
    """
    Crontinel monitoring client.

    Args:
        api_key: Your Crontinel API key (or set CRONTINEL_API_KEY env var).
        api_url: Crontinel API URL. Defaults to https://app.crontinel.com.
        app_name: Name of this application. Defaults to "python".
        sync: Use the ``requests`` library for HTTP. Set ``False`` to use ``httpx`` if installed.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_url: str | None = None,
        app_name: str = "python",
        sync: bool = True,
    ):
        self.api_key = api_key or os.environ.get("CRONTINEL_API_KEY")
        if not self.api_key:
            raise ValueError("Crontinel: api_key is required")
        self.api_url = (api_url or os.environ.get("CRONTINEL_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.app_name = app_name
        self.sync = sync

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        import json

        payload = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": method,
            "params": params,
        }

        if self.sync:
            try:
                import requests
            except ImportError:
                raise ImportError("Install crontinel with httpx: pip install 'crontinel[httpx]'")
            resp = requests.post(
                f"{self.api_url}/api/mcp",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                    "User-Agent": f"crontinel-python:{__version__}",
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        else:
            try:
                import httpx
            except ImportError:
                raise ImportError("Install crontinel with httpx: pip install 'crontinel[httpx]'")
            with httpx.Client() as client:
                resp = client.post(
                    f"{self.api_url}/api/mcp",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {self.api_key}",
                        "User-Agent": f"crontinel-python:{__version__}",
                    },
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()

        if "error" in data:
            raise RuntimeError(f"Crontinel RPC error: {data['error']}")
        return data.get("result")

    def schedule_run(
        self,
        command: str,
        duration_ms: int | None = None,
        exit_code: int = 0,
        ran_at: str | None = None,
    ) -> None:
        """
        Report a scheduled command run.

        Args:
            command: The command name, e.g. ``"reports:generate"``.
            duration_ms: How long the command took in milliseconds.
            exit_code: 0 for success, 1 for failure.
            ran_at: ISO 8601 timestamp. Defaults to now.
        """
        self._request("notify/schedule_run", {
            "command": command,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
            "ran_at": ran_at or self._now(),
            "app": self.app_name,
        })

    def queue_processed(
        self,
        queue: str,
        processed: int = 0,
        failed: int = 0,
        duration_ms: int | None = None,
        ran_at: str | None = None,
    ) -> None:
        """
        Report queue worker activity.

        Args:
            queue: Queue name, e.g. ``"emails"``.
            processed: Number of successfully processed jobs.
            failed: Number of failed jobs.
            duration_ms: How long the batch took in milliseconds.
            ran_at: ISO 8601 timestamp. Defaults to now.
        """
        self._request("notify/queue_processed", {
            "queue": queue,
            "processed": processed,
            "failed": failed,
            "duration_ms": duration_ms,
            "ran_at": ran_at or self._now(),
            "app": self.app_name,
        })

    def event(
        self,
        key: str,
        message: str,
        state: str = "info",
        metadata: dict[str, Any] | None = None,
        ran_at: str | None = None,
    ) -> None:
        """
        Send a custom alert or informational event.

        Args:
            key: Alert key, e.g. ``"disk-space-warning"``.
            message: Human-readable message.
            state: ``"firing"``, ``"resolved"``, or ``"info"``.
            metadata: Extra key-value data.
            ran_at: ISO 8601 timestamp. Defaults to now.
        """
        self._request("notify/event", {
            "key": key,
            "message": message,
            "state": state,
            "metadata": metadata or {},
            "ran_at": ran_at or self._now(),
            "app": self.app_name,
        })

    def monitor_schedule(
        self,
        command: str,
        fn: Callable[..., T],
        *args: Any,
        **kwargs: Any,
    ) -> tuple[T, int, int]:
        """
        Run ``fn`` and automatically report its outcome as a scheduled command.

        Returns:
            A tuple of ``(result, duration_ms, exit_code)``.

        Example::

            client = Crontinel(api_key="...")
            result, ms, code = client.monitor_schedule("my-task", my_task_function)
        """
        start = time.perf_counter()
        exit_code = 0
        try:
            result = fn(*args, **kwargs)
        except Exception:
            exit_code = 1
            raise
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            try:
                self.schedule_run(command, duration_ms=duration_ms, exit_code=exit_code)
            except Exception:
                pass  # Don't fail the caller's job if reporting fails
        return result, duration_ms, exit_code

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


T = TypeVar("T")
