"""Unit tests for terminal service get_working_directory function."""

from unittest.mock import Mock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    delete_terminal,
    get_working_directory,
)


class TestTerminalServiceWorkingDirectory:
    """Test terminal service working directory functionality."""

    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_success(self, mock_get_metadata, mock_tmux_client):
        """Test successful working directory retrieval."""
        # Arrange
        terminal_id = "test-terminal-123"
        expected_dir = "/home/user/project"
        mock_get_metadata.return_value = {
            "tmux_session": "test-session",
            "tmux_window": "test-window",
        }
        mock_tmux_client.get_pane_working_directory.return_value = expected_dir

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result == expected_dir
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_tmux_client.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window"
        )


class TestTerminalServiceStartupMapping:
    """Test startup mapping capture behavior for JSONL providers."""

    @patch("cli_agent_orchestrator.services.terminal_service.CAO_JSONL_ENABLED", True)
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.jsonl_status_engine")
    def test_create_terminal_captures_mapping_for_codex(
        self,
        mock_jsonl_engine,
        mock_provider_manager,
        mock_tmux_client,
        mock_generate_terminal_id,
        mock_generate_window_name,
        mock_db_create_terminal,
    ):
        mock_generate_terminal_id.return_value = "abc123ef"
        mock_generate_window_name.return_value = "win-codex"
        mock_tmux_client.session_exists.return_value = True
        mock_tmux_client.create_window.return_value = "win-codex"
        mock_tmux_client.get_pane_working_directory.return_value = "/home/user/project"
        mock_provider = Mock()
        mock_provider.initialize.return_value = True
        mock_provider.get_provider_session_hint.return_value = None
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_jsonl_engine.capture_startup_mapping.return_value = Mock(
            deterministic=True,
            session_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            reason_code="codex_single_matching_session",
        )

        terminal = create_terminal(
            provider=ProviderType.CODEX.value,
            agent_profile="developer",
            session_name="session-a",
            new_session=False,
            working_directory="/home/user/project",
        )

        assert terminal.id == "abc123ef"
        mock_jsonl_engine.capture_startup_mapping.assert_called_once_with(
            ProviderType.CODEX.value,
            "abc123ef",
            "session-a",
            "win-codex",
        )
        mock_db_create_terminal.assert_called_once()

    @patch("cli_agent_orchestrator.services.terminal_service.CAO_JSONL_ENABLED", True)
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.jsonl_status_engine")
    def test_create_terminal_skips_mapping_for_non_jsonl_provider(
        self,
        mock_jsonl_engine,
        mock_provider_manager,
        mock_tmux_client,
        mock_generate_terminal_id,
        mock_generate_window_name,
        mock_db_create_terminal,
    ):
        mock_generate_terminal_id.return_value = "abc123ef"
        mock_generate_window_name.return_value = "win-q"
        mock_tmux_client.session_exists.return_value = True
        mock_tmux_client.create_window.return_value = "win-q"
        mock_tmux_client.get_pane_working_directory.return_value = "/home/user/project"
        mock_provider = Mock()
        mock_provider.initialize.return_value = True
        mock_provider.get_provider_session_hint.return_value = None
        mock_provider_manager.create_provider.return_value = mock_provider

        terminal = create_terminal(
            provider=ProviderType.Q_CLI.value,
            agent_profile="developer",
            session_name="session-a",
            new_session=False,
            working_directory="/home/user/project",
        )

        assert terminal.id == "abc123ef"
        mock_jsonl_engine.capture_startup_mapping.assert_not_called()
        mock_db_create_terminal.assert_called_once()

    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_terminal_not_found(self, mock_get_metadata, mock_tmux_client):
        """Test ValueError when terminal not found."""
        # Arrange
        terminal_id = "nonexistent-terminal"
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'nonexistent-terminal' not found"):
            get_working_directory(terminal_id)

        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_tmux_client.get_pane_working_directory.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_returns_none(self, mock_get_metadata, mock_tmux_client):
        """Test when pane has no working directory."""
        # Arrange
        terminal_id = "test-terminal-456"
        mock_get_metadata.return_value = {
            "tmux_session": "test-session",
            "tmux_window": "test-window",
        }
        mock_tmux_client.get_pane_working_directory.return_value = None

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result is None
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_tmux_client.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window"
        )

    @patch("cli_agent_orchestrator.services.terminal_service.count_pending_approvals_without_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.resolve_pending_approvals_for_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_attempts_approval_cleanup_when_db_delete_raises(
        self,
        mock_get_metadata,
        mock_tmux_client,
        mock_provider_manager,
        mock_db_delete,
        mock_resolve_approvals,
        mock_count_orphans,
    ):
        mock_get_metadata.return_value = {"tmux_session": "s1", "tmux_window": "w1"}
        mock_db_delete.side_effect = RuntimeError("db unavailable")
        mock_resolve_approvals.return_value = 1
        mock_count_orphans.return_value = 0

        with pytest.raises(RuntimeError, match="db unavailable"):
            delete_terminal("term-1")

        mock_tmux_client.stop_pipe_pane.assert_called_once_with("s1", "w1")
        mock_provider_manager.cleanup_provider.assert_called_once_with("term-1")
        mock_resolve_approvals.assert_called_once_with(
            "term-1",
            resolution_message="terminal-deleted",
        )
