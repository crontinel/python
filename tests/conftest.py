"""Pytest configuration and fixtures."""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("CRONTINEL_API_KEY", "ctn_test_key_123")


@pytest.fixture
def mock_requests_post():
    """Mock requests.post for sync tests."""
    with patch("requests.post") as mock:
        mock.return_value = MagicMock(
            ok=True,
            status_code=200,
            json=lambda: {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}},
        )
        mock.return_value.raise_for_status = MagicMock()
        yield mock