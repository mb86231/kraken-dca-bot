"""Tests for the uvicorn web server launcher."""

from __future__ import annotations

from unittest.mock import patch


def test_start_web_server_starts_thread():
    from web.server import start_web_server

    with patch("uvicorn.run") as mock_uvicorn:
        thread = start_web_server(daemon=True)
        assert thread.name == "dca-web-server"
        assert thread.daemon is True
        # Give the thread a moment to start and call uvicorn.run.
        thread.join(timeout=0.5)
        mock_uvicorn.assert_called_once()
