"""Tests for crontinel Python SDK."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from crontinel import Crontinel  # noqa: E402


class TestCrontinel:
    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("CRONTINEL_API_KEY", raising=False)
        with pytest.raises(ValueError, match="api_key is required"):
            Crontinel(api_key=None)

    def test_requires_non_empty_api_key(self, monkeypatch):
        monkeypatch.delenv("CRONTINEL_API_KEY", raising=False)
        with pytest.raises(ValueError, match="api_key is required"):
            Crontinel(api_key="")

    def test_default_api_url(self):
        c = Crontinel(api_key="test")
        assert c.api_url == "https://app.crontinel.com"

    def test_custom_api_url(self):
        c = Crontinel(api_key="test", api_url="https://custom.example.com")
        assert c.api_url == "https://custom.example.com"

    def test_default_app_name(self):
        c = Crontinel(api_key="test")
        assert c.app_name == "python"

    def test_custom_app_name(self):
        c = Crontinel(api_key="test", app_name="my-worker")
        assert c.app_name == "my-worker"

    def test_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("CRONTINEL_API_KEY", "env_test_key")
        c = Crontinel()  # no explicit key
        assert c.api_key == "env_test_key"

    def test_schedule_run_payload(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")
        c.schedule_run(command="python manage.py run_task", duration_ms=1500, exit_code=0)

        mock_requests_post.assert_called_once()
        call_kwargs = mock_requests_post.call_args.kwargs
        body = call_kwargs["json"]
        assert body["method"] == "notify/schedule_run"
        assert body["params"]["command"] == "python manage.py run_task"
        assert body["params"]["duration_ms"] == 1500
        assert body["params"]["exit_code"] == 0
        assert body["params"]["app"] == "python"

    def test_schedule_run_default_exit_code(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")
        c.schedule_run(command="test", duration_ms=100)
        body = mock_requests_post.call_args.kwargs["json"]
        assert body["params"]["exit_code"] == 0

    def test_schedule_run_raises_on_error(self, mock_requests_post):
        mock_requests_post.return_value.raise_for_status.side_effect = Exception("401 Unauthorized")
        c = Crontinel(api_key="test")
        with pytest.raises(Exception, match="401"):
            c.schedule_run(command="test", duration_ms=100, exit_code=0)

    def test_queue_processed_payload(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")
        c.queue_processed(queue="emails", processed=50, failed=2, duration_ms=3200)
        body = mock_requests_post.call_args.kwargs["json"]
        assert body["method"] == "notify/queue_processed"
        assert body["params"]["queue"] == "emails"
        assert body["params"]["processed"] == 50
        assert body["params"]["failed"] == 2
        assert body["params"]["duration_ms"] == 3200

    def test_queue_processed_defaults(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")
        c.queue_processed(queue="default", duration_ms=100)
        body = mock_requests_post.call_args.kwargs["json"]
        assert body["params"]["processed"] == 0
        assert body["params"]["failed"] == 0

    def test_event_payload(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")
        c.event(key="deployment", message="Application deployed", state="info", metadata={"version": "2.1.0"})
        body = mock_requests_post.call_args.kwargs["json"]
        assert body["method"] == "notify/event"
        assert body["params"]["key"] == "deployment"
        assert body["params"]["message"] == "Application deployed"
        assert body["params"]["state"] == "info"
        assert body["params"]["metadata"]["version"] == "2.1.0"

    def test_event_default_state(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")
        c.event(key="test", message="Test event")
        body = mock_requests_post.call_args.kwargs["json"]
        assert body["params"]["state"] == "info"

    def test_monitor_schedule_calls_schedule_run(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")
        result, ms, code = c.monitor_schedule("my-task", lambda: "success")
        assert result == "success"
        assert isinstance(ms, int)
        assert ms >= 0
        assert code == 0
        body = mock_requests_post.call_args.kwargs["json"]
        assert body["method"] == "notify/schedule_run"
        assert body["params"]["command"] == "my-task"

    def test_monitor_schedule_reports_exit_code_1_on_error(self, mock_requests_post):
        c = Crontinel(api_key="ctn_test_key_123")

        def failing_task():
            raise ValueError("Task failed")

        with pytest.raises(ValueError, match="Task failed"):
            c.monitor_schedule("failing-task", failing_task)

        # finally block calls schedule_run even after error
        body = mock_requests_post.call_args.kwargs["json"]
        assert body["method"] == "notify/schedule_run"
        assert body["params"]["exit_code"] == 1


class TestCrontinelAsync:
    def test_async_init(self):
        c = Crontinel(api_key="test", sync=False)
        assert c.sync is False