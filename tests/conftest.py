"""Pytest configuration and fixtures."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

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