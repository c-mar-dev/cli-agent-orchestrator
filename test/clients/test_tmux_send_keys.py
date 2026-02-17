"""Tests for TmuxClient.send_keys literal implementation."""

from unittest.mock import call, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


@pytest.fixture
def client():
    with patch("cli_agent_orchestrator.clients.tmux.libtmux"):
        return TmuxClient()


@pytest.fixture
def mock_subprocess():
    with patch("cli_agent_orchestrator.clients.tmux.subprocess") as mock:
        mock.run.return_value = None
        yield mock


class TestSendKeys:
    """Tests for the literal send-keys implementation."""

    def test_basic_message(self, client, mock_subprocess):
        """Sends literal message and Enter."""
        client.send_keys("sess", "win", "hello")

        assert mock_subprocess.run.call_count == 2
        calls = mock_subprocess.run.call_args_list

        # literal send-keys
        assert calls[0] == call(
            ["tmux", "send-keys", "-t", "sess:win", "-l", "hello"],
            check=True,
        )
        # send Enter
        assert calls[1] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )

    def test_multiline_message(self, client, mock_subprocess):
        """Multi-line content is sent as a single literal payload."""
        msg = "line 1\nline 2\nline 3"
        client.send_keys("sess", "win", msg)

        send_call = mock_subprocess.run.call_args_list[0]
        assert send_call == call(["tmux", "send-keys", "-t", "sess:win", "-l", msg], check=True)

    def test_special_characters(self, client, mock_subprocess):
        """Quotes, backticks, dollars are sent raw (no key interpretation)."""
        msg = """He said "hello" and ran `cmd` with $VAR"""
        client.send_keys("sess", "win", msg)

        send_call = mock_subprocess.run.call_args_list[0]
        assert send_call[0][0] == ["tmux", "send-keys", "-t", "sess:win", "-l", msg]

    def test_empty_message(self, client, mock_subprocess):
        """Empty string means just press Enter (no literal payload chunk)."""
        client.send_keys("sess", "win", "")

        assert mock_subprocess.run.call_count == 1
        assert mock_subprocess.run.call_args_list[0] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )

    def test_error_bubbles_up(self, client, mock_subprocess):
        """Exceptions from tmux calls are surfaced to caller."""
        mock_subprocess.run.side_effect = [
            Exception("send failed"),
        ]

        with pytest.raises(Exception, match="send failed"):
            client.send_keys("sess", "win", "msg")

    def test_large_message(self, client, mock_subprocess, monkeypatch):
        """Large messages are chunked into bounded literal send-keys calls."""
        monkeypatch.setenv("CAO_TMUX_LITERAL_CHUNK_SIZE", "4096")
        msg = "X" * 50000
        client.send_keys("sess", "win", msg)

        expected_chunks = (len(msg) + 4095) // 4096
        assert mock_subprocess.run.call_count == expected_chunks + 1

        send_calls = mock_subprocess.run.call_args_list[:-1]
        reconstructed = ""
        for send_call in send_calls:
            args = send_call[0][0]
            assert args[0:5] == ["tmux", "send-keys", "-t", "sess:win", "-l"]
            reconstructed += args[5]

        assert reconstructed == msg
        assert mock_subprocess.run.call_args_list[-1] == call(
            ["tmux", "send-keys", "-t", "sess:win", "Enter"],
            check=True,
        )
