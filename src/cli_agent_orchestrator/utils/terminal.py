"""Session utilities for CLI Agent Orchestrator."""

import logging
import os
import time
import uuid
from typing import TYPE_CHECKING, Optional, Sequence

import httpx

from cli_agent_orchestrator.constants import API_BASE_URL, SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import TerminalStatus

if TYPE_CHECKING:
    from cli_agent_orchestrator.clients.tmux import TmuxClient
    from cli_agent_orchestrator.providers.base import BaseProvider

logger = logging.getLogger(__name__)
API_STATUS_REQUEST_TIMEOUT = float(os.getenv("CAO_API_STATUS_REQUEST_TIMEOUT_SECONDS", "10"))


def _api_base_url() -> str:
    """Resolve API base URL at call time so tests can override it via env."""
    return os.getenv("CAO_API_BASE_URL", API_BASE_URL).rstrip("/")


def _api_url(path: str) -> str:
    """Build full API URL from path."""
    return f"{_api_base_url()}{path if path.startswith('/') else '/' + path}"


def generate_session_name() -> str:
    """Generate a unique session name with SESSION_PREFIX."""
    session_uuid = uuid.uuid4().hex[:8]
    return f"{SESSION_PREFIX}{session_uuid}"


def generate_terminal_id() -> str:
    """Generate terminal ID without prefix."""
    return uuid.uuid4().hex[:8]


def generate_window_name(agent_profile: str) -> str:
    """Generate window name from agent profile with unique suffix."""
    return f"{agent_profile}-{uuid.uuid4().hex[:4]}"


def wait_for_shell(
    tmux_client: "TmuxClient",
    session_name: str,
    window_name: str,
    timeout: float = 10.0,
    polling_interval: float = 0.5,
) -> bool:
    """Wait for shell to be ready by checking if output is stable (2 consecutive reads are the same and non-empty)."""
    logger.info(f"Waiting for shell to be ready in {session_name}:{window_name}...")
    start_time = time.time()
    previous_output = None

    while time.time() - start_time < timeout:
        output = tmux_client.get_history(session_name, window_name)

        if output and output.strip() and previous_output is not None and output == previous_output:
            logger.info(f"Shell ready")
            return True

        previous_output = output
        time.sleep(polling_interval)

    logger.warning(f"Timeout waiting for shell to be ready")
    return False


def wait_until_status(
    provider_instance: "BaseProvider",
    target_status: TerminalStatus,
    acceptable_statuses: Optional[Sequence[TerminalStatus]] = None,
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until provider reaches target status or timeout."""
    start_time = time.time()
    acceptable_statuses = acceptable_statuses or ()

    while time.time() - start_time < timeout:
        status = provider_instance.get_status()
        logger.info(f"Waiting for {target_status}, current status: {status}")
        if status == target_status or status in acceptable_statuses:
            return True
        time.sleep(polling_interval)

    return False


def wait_until_terminal_status(
    terminal_id: str,
    target_status: TerminalStatus,
    timeout: float = 30.0,
    polling_interval: float = 1.0,
    blocked_statuses: Optional[Sequence[TerminalStatus]] = None,
) -> bool:
    """Wait until terminal reaches target status using API endpoint."""
    start_time = time.time()
    blocked_status_values = {status.value for status in blocked_statuses or ()}

    while time.time() - start_time < timeout:
        try:
            response = httpx.get(
                _api_url(f"/terminals/{terminal_id}"), timeout=API_STATUS_REQUEST_TIMEOUT
            )
            if response.status_code == 200:
                terminal_data = response.json()
                current_status = terminal_data.get("status")
                if current_status == target_status.value:
                    return True
                if current_status in blocked_status_values:
                    logger.warning(
                        "Terminal %s reached blocked status %s while waiting for %s",
                        terminal_id,
                        current_status,
                        target_status.value,
                    )
                    return False
        except Exception:
            pass
        time.sleep(polling_interval)
    return False
