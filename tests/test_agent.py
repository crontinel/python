"""Tests for the Crontinel agent daemon."""

from __future__ import annotations

import os
import threading
import time
from unittest.mock import patch

import pytest

from crontinel.agent import Agent, SseEvent


# ── SSE Parsing ────────────────────────────────────────────────────────────────


def test_parses_complete_command_event():
    """Single complete command event is dispatched."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    original_handler = agent.handle_command_event
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    agent.feed_sse_data(
        'event: command\n'
        'data: {"command_id":"abc123","command":"php artisan inspire"}\n'
        '\n'
    )

    assert len(events) == 1
    assert "abc123" in events[0]


def test_parses_multiple_events_in_sequence():
    """Multiple SSE events are parsed one by one."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    sse = (
        "event: ping\n"
        "data: {}\n"
        "\n"
        "event: command\n"
        'data: {"command_id":"xyz","command":"ls -la"}\n'
        "\n"
        "event: ping\n"
        "data: {}\n"
        "\n"
    )
    agent.feed_sse_data(sse)

    assert len(events) == 1  # only command events are dispatched to handle_command_event
    assert "xyz" in events[0]


def test_handles_multiline_data_fields():
    """Multiline data field is concatenated properly."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    agent.feed_sse_data(
        "event: command\n"
        'data: {"command_id":"m1",\n'
        'data: "command":"echo hello"}\n'
        "\n"
    )

    assert len(events) == 1
    assert "m1" in events[0]
    assert "echo hello" in events[0]


def test_ignores_comments():
    """Lines starting with ':' (SSE comments) are ignored."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    agent.feed_sse_data(": this is a comment\n\n")

    assert len(events) == 0


def test_handles_partial_data_across_multiple_calls():
    """Data arriving in chunks is reassembled correctly."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    agent.feed_sse_data("event: command\nda")
    agent.feed_sse_data('ta: {"command_id":"p1",')
    agent.feed_sse_data('"command":"whoami"}' + "\n\n")

    assert len(events) == 1
    assert "p1" in events[0]


def test_ignores_unknown_event_types():
    """Events with unknown types are silently discarded."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    agent.feed_sse_data(
        "event: unknown\n"
        'data: {"foo":"bar"}\n'
        "\n"
    )

    assert len(events) == 0


def test_sse_event_defaults():
    """SseEvent has empty defaults."""
    ev = SseEvent()
    assert ev.event == ""
    assert ev.data == ""
    assert ev.id == ""


# ── Command Event Handling ─────────────────────────────────────────────────────


def test_handle_command_reports_result():
    """Command execution triggers a result report."""
    agent = Agent(api_key="test-key")
    reported: list[dict] = []
    agent._report_command_result = lambda **kw: reported.append(kw)  # type: ignore[assignment]

    agent.handle_command_event(
        '{"command_id":"cmd1","command":"echo hello"}'
    )

    # The command should have been executed and reported
    assert len(reported) == 1
    assert reported[0]["command_id"] == "cmd1"
    assert reported[0]["status"] == "completed"
    assert reported[0]["exit_code"] == 0
    # Output should contain "hello"
    assert "hello" in reported[0]["output"]
    # All required fields present
    for field in [
        "command_id", "status", "exit_code", "output",
        "started_at", "finished_at", "duration_ms",
    ]:
        assert field in reported[0]


def test_handle_command_reports_failure():
    """Non-zero exit command is reported as failed."""
    agent = Agent(api_key="test-key")
    reported: list[dict] = []
    agent._report_command_result = lambda **kw: reported.append(kw)  # type: ignore[assignment]

    agent.handle_command_event(
        '{"command_id":"cmd2","command":"exit 42"}'
    )

    assert len(reported) == 1
    assert reported[0]["command_id"] == "cmd2"
    assert reported[0]["status"] == "failed"
    assert reported[0]["exit_code"] == 42


def test_handle_malformed_json():
    """Malformed command event JSON does not trigger execution."""
    agent = Agent(api_key="test-key")
    reported: list[dict] = []
    agent._report_command_result = lambda **kw: reported.append(kw)  # type: ignore[assignment]

    agent.handle_command_event("not-json")

    assert len(reported) == 0


def test_handle_missing_required_fields():
    """Command event missing command_id or command is ignored."""
    agent = Agent(api_key="test-key")
    reported: list[dict] = []
    agent._report_command_result = lambda **kw: reported.append(kw)  # type: ignore[assignment]

    agent.handle_command_event('{"event":"test"}')

    assert len(reported) == 0


def test_handle_command_includes_env_overrides(tmp_path):
    """Env overrides from the command payload are passed to the subprocess."""
    agent = Agent(api_key="test-key")
    reported: list[dict] = []
    agent._report_command_result = lambda **kw: reported.append(kw)  # type: ignore[assignment]

    agent.handle_command_event(
        '{"command_id":"cmd3","command":"echo $MY_VAR","env":{"MY_VAR":"custom"}}'
    )

    assert len(reported) == 1
    assert reported[0]["status"] == "completed"
    assert "custom" in reported[0]["output"]


# ── Heartbeat ──────────────────────────────────────────────────────────────────


def test_heartbeat_triggers_when_due():
    """Heartbeat is sent when the interval has elapsed."""
    agent = Agent(api_key="test-key")
    agent.started_at = time.time() - 120  # started 120s ago
    agent.last_heartbeat_at = time.time() - 120  # last heartbeat 120s ago
    sent: list[dict] = []
    agent._send_heartbeat = lambda: sent.append(True)  # type: ignore[assignment]

    agent._check_heartbeat()

    assert len(sent) == 1


def test_heartbeat_not_sent_before_interval():
    """Heartbeat is not sent before the interval elapses."""
    agent = Agent(api_key="test-key")
    agent.started_at = time.time()
    agent.last_heartbeat_at = time.time()
    sent: list[dict] = []
    agent._send_heartbeat = lambda: sent.append(True)  # type: ignore[assignment]

    agent._check_heartbeat()

    assert len(sent) == 0


# ── Configuration ──────────────────────────────────────────────────────────────


def test_is_not_configured_when_api_key_empty():
    """is_configured returns False for empty key."""
    # Clear all env so no CRONTINEL_API_KEY fallback
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="CRONTINEL_API_KEY"):
            Agent(api_key="")


def test_is_configured_when_api_key_set():
    """is_configured returns True when key is provided."""
    agent = Agent(api_key="some-key")
    assert agent.is_configured() is True


def test_requires_api_key():
    """Constructor raises ValueError without an API key."""
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(ValueError, match="CRONTINEL_API_KEY"):
            Agent(api_key=None)


def test_api_url_default():
    """Default API URL is used when none is provided."""
    with patch.dict(os.environ, {"CRONTINEL_API_KEY": "test"}, clear=True):
        agent = Agent()
        assert "app.crontinel.com" in agent.api_url


def test_api_url_from_env():
    """API URL from env var is respected."""
    with patch.dict(os.environ, {
        "CRONTINEL_API_KEY": "test",
        "CRONTINEL_API_URL": "https://custom.example.com",
    }, clear=True):
        agent = Agent()
        assert "custom.example.com" in agent.api_url


def test_api_url_strips_trailing_api():
    """Trailing /api is stripped from the base URL."""
    agent = Agent(api_key="test", api_url="https://app.crontinel.com/api")
    assert agent.api_url == "https://app.crontinel.com"


# ── SSE Parser Edge Cases ──────────────────────────────────────────────────────


def test_line_without_colon_skipped():
    """Lines without a colon are skipped."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    agent.feed_sse_data("data\n\n")
    assert agent.current_event is None


def test_trailing_blank_lines():
    """Multiple trailing blank lines don't crash the parser."""
    agent = Agent(api_key="test-key")
    events: list[str] = []
    agent.handle_command_event = lambda d: events.append(d)  # type: ignore[assignment]

    agent.feed_sse_data(
        "event: command\n"
        'data: {"command_id":"t","command":"echo x"}\n'
        "\n"
        "\n"
        "\n"
    )

    assert len(events) == 1


# ── Signal Handling ────────────────────────────────────────────────────────────


def test_signal_stops_running_loop():
    """SIGINT sets running to False via installed handler."""
    agent = Agent(api_key="test-key")
    agent._install_signal_handlers()
    assert agent.running is True

    import signal
    signal.raise_signal(signal.SIGINT)

    assert agent.running is False


def test_sigterm_stops_running_loop():
    """SIGTERM sets running to False via installed handler."""
    agent = Agent(api_key="test-key")
    agent._install_signal_handlers()
    assert agent.running is True

    import signal
    signal.raise_signal(signal.SIGTERM)

    assert agent.running is False
