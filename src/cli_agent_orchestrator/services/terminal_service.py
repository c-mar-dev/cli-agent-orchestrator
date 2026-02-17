"""Terminal service with workflow functions."""

import logging
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    count_pending_approvals_without_terminal,
    get_terminal_metadata,
    resolve_pending_approvals_for_terminal,
    update_last_active,
    update_terminal_mapping,
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import (
    CAO_JSONL_ENABLED,
    DEFAULT_WORKING_DIRECTORY,
    SESSION_PREFIX,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.parsing.jsonl_status_engine import jsonl_status_engine
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
)

logger = logging.getLogger(__name__)


class OutputMode(str, Enum):
    """Output mode for terminal history."""

    FULL = "full"
    LAST = "last"


def create_terminal(
    provider: str,
    agent_profile: str,
    session_name: Optional[str] = None,
    new_session: bool = False,
    working_directory: Optional[str] = None,
) -> Terminal:
    """Create terminal, optionally creating new session with it."""
    try:
        terminal_id = generate_terminal_id()
        resolved_working_directory = working_directory or DEFAULT_WORKING_DIRECTORY

        # Generate session name if not provided
        if not session_name:
            session_name = generate_session_name()

        window_name = generate_window_name(agent_profile)

        if new_session:
            # Apply SESSION_PREFIX if not already present
            if not session_name.startswith(SESSION_PREFIX):
                session_name = f"{SESSION_PREFIX}{session_name}"

            # Check if session already exists
            if tmux_client.session_exists(session_name):
                raise ValueError(f"Session '{session_name}' already exists")

            # Create new tmux session with this terminal as the initial window
            tmux_client.create_session(
                session_name,
                window_name,
                terminal_id,
                resolved_working_directory,
            )
        else:
            # Add window to existing session
            if not tmux_client.session_exists(session_name):
                raise ValueError(f"Session '{session_name}' not found")
            window_name = tmux_client.create_window(
                session_name,
                window_name,
                terminal_id,
                resolved_working_directory,
            )

        launch_cwd = tmux_client.get_pane_working_directory(session_name, window_name)
        if not launch_cwd:
            launch_cwd = resolved_working_directory

        # Save terminal metadata to database
        db_create_terminal(
            terminal_id,
            session_name,
            window_name,
            provider,
            agent_profile,
            launch_cwd=launch_cwd,
        )

        # Initialize provider
        provider_instance = provider_manager.create_provider(
            provider, terminal_id, session_name, window_name, agent_profile
        )
        provider_instance.initialize()
        provider_session_hint = provider_instance.get_provider_session_hint()
        if provider_session_hint:
            update_terminal_mapping(
                terminal_id,
                provider_session_id=provider_session_hint,
                status_reason_code="provider_session_hint",
            )
        if CAO_JSONL_ENABLED and provider in (
            ProviderType.CLAUDE_CODE.value,
            ProviderType.CODEX.value,
        ):
            mapping = jsonl_status_engine.capture_startup_mapping(
                provider,
                terminal_id,
                session_name,
                window_name,
            )
            if mapping.deterministic:
                logger.info(
                    "Captured deterministic provider session mapping terminal=%s session_id=%s",
                    terminal_id,
                    mapping.session_id,
                )
            else:
                logger.warning(
                    "Deterministic provider session mapping unavailable at startup for "
                    "terminal=%s reason=%s; staying in hybrid fallback mode",
                    terminal_id,
                    mapping.reason_code,
                )

        # Create log file and start pipe-pane
        log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
        log_path.touch()  # Ensure file exists before watching
        tmux_client.pipe_pane(session_name, window_name, str(log_path))

        terminal = Terminal(
            id=terminal_id,
            name=window_name,
            provider=ProviderType(provider),
            session_name=session_name,
            agent_profile=agent_profile,
            status=TerminalStatus.IDLE,
            last_active=datetime.now(),
        )

        logger.info(
            f"Created terminal: {terminal_id} in session: {session_name} (new_session={new_session})"
        )
        return terminal

    except Exception as e:
        logger.error(f"Failed to create terminal: {e}")
        if new_session and session_name:
            try:
                tmux_client.kill_session(session_name)
            except:
                pass
        raise


def get_terminal(terminal_id: str) -> Dict:
    """Get terminal data."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Get status from provider
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            raise ValueError(f"Provider not found for terminal {terminal_id}")
        status_enum = provider.get_status()
        from cli_agent_orchestrator.services import inbox_service  # local import avoids cycle

        refreshed_metadata = get_terminal_metadata(terminal_id) or metadata
        inbox_service.sync_approval_state_for_terminal(
            terminal_id,
            status_enum,
            metadata=refreshed_metadata,
            source="terminal-status",
        )
        refreshed_metadata = get_terminal_metadata(terminal_id) or refreshed_metadata
        status = status_enum.value

        return {
            "id": refreshed_metadata["id"],
            "name": refreshed_metadata["tmux_window"],
            "provider": refreshed_metadata["provider"],
            "session_name": refreshed_metadata["tmux_session"],
            "agent_profile": refreshed_metadata["agent_profile"],
            "status": status,
            "status_source": refreshed_metadata.get("status_source"),
            "mapping_confidence": refreshed_metadata.get("mapping_confidence"),
            "status_reason_code": refreshed_metadata.get("status_reason_code"),
            "last_active": refreshed_metadata["last_active"],
        }

    except Exception as e:
        logger.error(f"Failed to get terminal {terminal_id}: {e}")
        raise


def get_working_directory(terminal_id: str) -> Optional[str]:
    """Get the current working directory of a terminal's pane.

    Args:
        terminal_id: The terminal identifier

    Returns:
        Working directory path, or None if pane has no directory

    Raises:
        ValueError: If terminal not found
        Exception: If unable to query working directory
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        working_dir = tmux_client.get_pane_working_directory(
            metadata["tmux_session"], metadata["tmux_window"]
        )
        return working_dir

    except Exception as e:
        logger.error(f"Failed to get working directory for terminal {terminal_id}: {e}")
        raise


def send_input(terminal_id: str, message: str) -> bool:
    """Send input to terminal."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        tmux_client.send_keys(metadata["tmux_session"], metadata["tmux_window"], message)

        update_last_active(terminal_id)
        logger.info(f"Sent input to terminal: {terminal_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send input to terminal {terminal_id}: {e}")
        raise


def get_output(terminal_id: str, mode: OutputMode = OutputMode.FULL) -> str:
    """Get terminal output."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        full_output = tmux_client.get_history(metadata["tmux_session"], metadata["tmux_window"])

        if mode == OutputMode.FULL:
            return full_output
        elif mode == OutputMode.LAST:
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                raise ValueError(f"Provider not found for terminal {terminal_id}")
            return provider.extract_last_message_from_script(full_output)

    except Exception as e:
        logger.error(f"Failed to get output from terminal {terminal_id}: {e}")
        raise


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal."""
    try:
        # Get metadata before deletion
        metadata = get_terminal_metadata(terminal_id)
        cleanup_errors = []

        # Stop pipe-pane
        if metadata:
            try:
                tmux_client.stop_pipe_pane(metadata["tmux_session"], metadata["tmux_window"])
            except Exception as e:
                cleanup_errors.append(f"pipe-pane: {e}")
                logger.warning(f"Failed to stop pipe-pane for {terminal_id}: {e}")

        try:
            provider_manager.cleanup_provider(terminal_id)
        except Exception as e:
            cleanup_errors.append(f"provider-cleanup: {e}")
            logger.warning(f"Failed to cleanup provider for {terminal_id}: {e}")

        deleted = False
        db_delete_error: Exception | None = None
        try:
            deleted = db_delete_terminal(terminal_id)
        except Exception as e:
            cleanup_errors.append(f"db-delete: {e}")
            logger.error(f"Failed to delete terminal metadata for {terminal_id}: {e}")
            db_delete_error = e

        approvals_resolved = 0
        try:
            approvals_resolved = resolve_pending_approvals_for_terminal(
                terminal_id,
                resolution_message="terminal-deleted",
            )
        except Exception as e:
            cleanup_errors.append(f"approval-cleanup: {e}")
            logger.error("Failed to cleanup pending approvals for terminal %s: %s", terminal_id, e)

        if approvals_resolved > 0:
            logger.info(
                "Resolved %s pending approval(s) while deleting terminal %s",
                approvals_resolved,
                terminal_id,
            )
        orphan_pending = count_pending_approvals_without_terminal()
        if orphan_pending:
            logger.warning(
                "Pending approvals reference missing terminals count=%s after deleting %s",
                orphan_pending,
                terminal_id,
            )
        if cleanup_errors:
            logger.warning(
                "Terminal delete completed with non-fatal cleanup errors terminal=%s errors=%s",
                terminal_id,
                "; ".join(cleanup_errors),
            )
        if db_delete_error is not None:
            raise db_delete_error
        logger.info(f"Deleted terminal: {terminal_id}")
        return deleted

    except Exception as e:
        logger.error(f"Failed to delete terminal {terminal_id}: {e}")
        raise
