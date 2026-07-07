"""Tests for the Crontinel agent daemon."""

from __future__ import annotations

import json
import os
import signal
import socket
import threading
import time
from unittest.mock import patch

import pytest

from crontinel.agent import Agent


# ── Configuration ────────────────────────────────────────────────────────────────


def test_is_not_configured_when_api_key_empty():
    """is_configured returns False for empty key."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="CRONTINEL_API_KEY"):
            Agent(api_key="")


def test_is_not_configured_when_app_id_empty():
    """is_configured returns False for empty app_id."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="CRONTINEL_APP_ID"):
            Agent(api_key="some-key", app_id="")


def test_is_configured_when_both_set():
    """is_configured returns True when key and app_id are provided."""
    agent = Agent(api_key="some-key", app_id="my-app")
    assert agent.is_configured() is True


def test_requires_api_key():
    """Constructor raises ValueError without an API key."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="CRONTINEL_API_KEY"):
            Agent(api_key=None)


def test_requires_app_id():
    """Constructor raises ValueError without an app_id."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="CRONTINEL_APP_ID"):
            Agent(api_key="key", app_id=None)


def test_api_url_default():
    """Default API URL is used when none is provided."""
    with patch.dict(os.environ, {"CRONTINEL_API_KEY": "test", "CRONTINEL_APP_ID": "my-app"}, clear=True):
        agent = Agent()
        assert "app.crontinel.com" in agent.api_url


def test_api_url_from_env():
    """API URL from env var is respected."""
    with patch.dict(os.environ, {
        "CRONTINEL_API_KEY": "test",
        "CRONTINEL_APP_ID": "my-app",
        "CRONTINEL_API_URL": "https://custom.example.com",
    }, clear=True):
        agent = Agent()
        assert "custom.example.com" in agent.api_url


def test_api_url_strips_trailing_api():
    """Trailing /api is stripped from the base URL."""
    agent = Agent(api_key="test", app_id="my-app", api_url="https://app.crontinel.com/api")
    assert agent.api_url == "https://app.crontinel.com"


# ── Agent Identity ───────────────────────────────────────────────────────────────


def test_agent_id_from_hostname():
    """Agent ID matches the hostname."""
    agent = Agent(api_key="test", app_id="my-app")
    assert agent._agent_id() == socket.gethostname()


def test_agent_name_prefixed():
    """Agent name is prefixed with python-agent."""
    agent = Agent(api_key="test", app_id="my-app")
    assert agent._agent_name() == f"python-agent:{socket.gethostname()}"


# ── Registration ─────────────────────────────────────────────────────────────────


def test_register_sends_request():
    """Registration sends POST to /v1/agents/register."""
    agent = Agent(api_key="test-key", app_id="test-app")
    sent_urls = []
    sent_bodies = []

    original_post = agent._http_post

    def fake_post(url, json_body):
        sent_urls.append(url)
        sent_bodies.append(json_body)
        return {"id": 1, "status": "connected"}

    agent._http_post = fake_post  # type: ignore[assignment]
    agent.register()

    assert len(sent_urls) == 1
    assert "/v1/agents/register" in sent_urls[0]
    assert sent_bodies[0]["agent_id"] == socket.gethostname()
    assert sent_bodies[0]["agent_name"] == f"python-agent:{socket.gethostname()}"


# ── Command Fetching ─────────────────────────────────────────────────────────────


def test_fetch_commands_no_commands():
    """Returns empty list when no commands pending."""
    agent = Agent(api_key="test-key", app_id="test-app")

    def fake_get(url):
        return {"data": []}

    agent._http_get = fake_get  # type: ignore[assignment]
    result = agent._fetch_commands()

    assert result == []


def test_fetch_commands_returns_list():
    """Returns commands when pending."""
    agent = Agent(api_key="test-key", app_id="test-app")

    def fake_get(url):
        return {
            "data": [
                {"id": 1, "command": "echo hello", "payload": None, "request_key": None},
            ]
        }

    agent._http_get = fake_get  # type: ignore[assignment]
    result = agent._fetch_commands()

    assert len(result) == 1
    assert result[0]["id"] == 1


# ── Command Execution ────────────────────────────────────────────────────────────


def test_execute_and_report_passes_correct_fields():
    """Command execution passes trigger_id to report."""
    agent = Agent(api_key="test-key", app_id="test-app")
    reported = []

    def fake_report(**kw):
        reported.append(kw)

    agent._report_result = fake_report  # type: ignore[assignment]

    agent._execute_and_report({
        "id": 42,
        "command": "echo hello",
        "payload": None,
        "request_key": None,
    })

    assert len(reported) >= 1
    # First should be acknowledgment
    assert reported[0]["status"] == "acknowledged"
    assert reported[0]["trigger_id"] == "42"
    # Second should be the result
    assert len(reported) >= 2
    assert reported[1]["status"] == "completed"
    assert reported[1]["exit_code"] == 0
    assert "hello" in reported[1]["output"]


def test_execute_reports_failure():
    """Non-zero exit command is reported as failed."""
    agent = Agent(api_key="test-key", app_id="test-app")
    reported = []

    def fake_report(**kw):
        reported.append(kw)

    agent._report_result = fake_report  # type: ignore[assignment]

    agent._execute_and_report({
        "id": 2,
        "command": "exit 42",
        "payload": None,
        "request_key": None,
    })

    # Find the result report (skip acknowledgment)
    result_reports = [r for r in reported if r["status"] != "acknowledged"]
    assert len(result_reports) == 1
    assert result_reports[0]["status"] == "failed"
    assert result_reports[0]["exit_code"] == 42


def test_execute_includes_env_overrides(tmp_path):
    """Env overrides from payload are passed to the subprocess."""
    agent = Agent(api_key="test-key", app_id="test-app")
    reported = []

    def fake_report(**kw):
        reported.append(kw)

    agent._report_result = fake_report  # type: ignore[assignment]

    agent._execute_and_report({
        "id": 3,
        "command": "echo $MY_VAR",
        "payload": {"MY_VAR": "custom"},
        "request_key": None,
    })

    result_reports = [r for r in reported if r["status"] != "acknowledged"]
    assert len(result_reports) == 1
    assert result_reports[0]["status"] == "completed"
    assert "custom" in result_reports[0]["output"]


# ── Heartbeat ────────────────────────────────────────────────────────────────────


def test_heartbeat_triggers_when_due():
    """Heartbeat is sent when the interval has elapsed."""
    agent = Agent(api_key="test-key", app_id="test-app")
    agent.started_at = time.time() - 120  # started 120s ago
    agent.last_heartbeat_at = time.time() - 120  # last heartbeat 120s ago
    sent = []

    def fake_heartbeat():
        sent.append(True)

    agent._send_heartbeat = fake_heartbeat  # type: ignore[assignment]

    agent._check_heartbeat()

    assert len(sent) == 1


def test_heartbeat_not_sent_before_interval():
    """Heartbeat is not sent before the interval elapses."""
    agent = Agent(api_key="test-key", app_id="test-app")
    agent.started_at = time.time()
    agent.last_heartbeat_at = time.time()
    sent = []

    def fake_heartbeat():
        sent.append(True)

    agent._send_heartbeat = fake_heartbeat  # type: ignore[assignment]

    agent._check_heartbeat()

    assert len(sent) == 0


# ── Poll Once ────────────────────────────────────────────────────────────────────


def test_poll_once_no_commands():
    """Poll returns early when no commands are pending."""
    agent = Agent(api_key="test-key", app_id="test-app")

    def fake_fetch():
        return []

    agent._fetch_commands = fake_fetch  # type: ignore[assignment]

    # Should not raise
    agent._poll_once()
    assert agent.running is True


# ── Result Reporting ─────────────────────────────────────────────────────────────


def test_report_result_contains_required_fields():
    """Required fields are included in the result report."""
    agent = Agent(api_key="test-key", app_id="test-app")
    sent_payloads = []

    def fake_post(url, json_body):
        sent_payloads.append(json_body)

    agent._http_post = fake_post  # type: ignore[assignment]

    agent._report_result(
        trigger_id="123",
        status="completed",
        exit_code=0,
        output="ok",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        duration_ms=1000,
    )

    assert len(sent_payloads) == 1
    payload = sent_payloads[0]
    assert payload["trigger_id"] == "123"
    assert payload["status"] == "completed"
    assert payload["exit_code"] == 0
    assert payload["output"] == "ok"


def test_report_result_minimal_payload():
    """Acknowledgment sends just the minimal fields."""
    agent = Agent(api_key="test-key", app_id="test-app")
    sent_payloads = []

    def fake_post(url, json_body):
        sent_payloads.append(json_body)

    agent._http_post = fake_post  # type: ignore[assignment]

    agent._report_result(trigger_id="456", status="acknowledged")

    assert len(sent_payloads) == 1
    payload = sent_payloads[0]
    assert payload["trigger_id"] == "456"
    assert payload["status"] == "acknowledged"
    # No optional fields
    assert "exit_code" not in payload
    assert "output" not in payload


# ── Signal Handling ──────────────────────────────────────────────────────────────


def test_signal_stops_running_loop():
    """SIGINT sets running to False via installed handler."""
    agent = Agent(api_key="test-key", app_id="test-app")
    agent._install_signal_handlers()
    assert agent.running is True

    signal.raise_signal(signal.SIGINT)

    assert agent.running is False


def test_sigterm_stops_running_loop():
    """SIGTERM sets running to False via installed handler."""
    agent = Agent(api_key="test-key", app_id="test-app")
    agent._install_signal_handlers()
    assert agent.running is True

    signal.raise_signal(signal.SIGTERM)

    assert agent.running is False
