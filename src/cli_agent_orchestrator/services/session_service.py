"""Session service for session-level operations."""

import logging
from typing import Dict, List

from cli_agent_orchestrator.clients.database import (
    list_terminals_by_session,
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)


def list_sessions() -> List[Dict]:
    """List all sessions from tmux."""
    try:
        tmux_sessions = tmux_client.list_sessions()
        return [s for s in tmux_sessions if s["id"].startswith(SESSION_PREFIX)]
    except Exception as e:
        logger.error(f"Failed to list sessions: {e}")
        return []


def get_session(session_name: str) -> Dict:
    """Get session with terminals."""
    try:
        if not tmux_client.session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        tmux_sessions = tmux_client.list_sessions()
        session_data = next((s for s in tmux_sessions if s["id"] == session_name), None)

        if not session_data:
            raise ValueError(f"Session '{session_name}' not found")

        terminals = list_terminals_by_session(session_name)
        return {"session": session_data, "terminals": terminals}

    except Exception as e:
        logger.error(f"Failed to get session {session_name}: {e}")
        raise


def delete_session(session_name: str) -> bool:
    """Delete session and cleanup."""
    try:
        if not tmux_client.session_exists(session_name):
            raise ValueError(f"Session '{session_name}' not found")

        terminals = list_terminals_by_session(session_name)

        # Per-terminal teardown includes provider cleanup + approval resolution.
        for terminal in terminals:
            terminal_id = terminal["id"]
            try:
                terminal_service.delete_terminal(terminal_id)
            except Exception as e:
                logger.warning(
                    "Terminal delete failed during session cleanup; falling back to provider cleanup "
                    "terminal=%s error=%s",
                    terminal_id,
                    e,
                )
                try:
                    provider_manager.cleanup_provider(terminal_id)
                except Exception as provider_error:
                    logger.warning(
                        "Fallback provider cleanup failed terminal=%s error=%s",
                        terminal_id,
                        provider_error,
                    )

        # Kill tmux session
        tmux_client.kill_session(session_name)

        logger.info(f"Deleted session: {session_name}")
        return True

    except Exception as e:
        logger.error(f"Failed to delete session {session_name}: {e}")
        raise
