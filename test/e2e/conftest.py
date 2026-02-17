"""E2E fixtures for real CAO server orchestration tests."""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import List

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


def _is_truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


E2E_HEALTH_ATTEMPTS = int(os.getenv("CAO_E2E_HEALTH_ATTEMPTS", "5"))
E2E_HEALTH_TIMEOUT = float(os.getenv("CAO_E2E_HEALTH_TIMEOUT", "5"))
E2E_HEALTH_INTERVAL_SECONDS = float(os.getenv("CAO_E2E_HEALTH_INTERVAL_SECONDS", "1"))
E2E_REQUEST_TIMEOUT = float(os.getenv("CAO_E2E_REQUEST_TIMEOUT", "45"))
E2E_PROVIDER = os.getenv("CAO_E2E_PROVIDER", "claude_code")
E2E_PROFILE = os.getenv("CAO_E2E_PROFILE", "developer")
E2E_PREFLIGHT_ENABLED = _is_truthy(os.getenv("CAO_E2E_PREFLIGHT_ENABLED", "true"))
E2E_PREFLIGHT_TIMEOUT = float(os.getenv("CAO_E2E_PREFLIGHT_TIMEOUT", "30"))
E2E_API_BASE_URL = os.getenv("CAO_E2E_API_BASE_URL", os.getenv("CAO_API_BASE_URL", API_BASE_URL))
E2E_API_BASE_URL = E2E_API_BASE_URL.rstrip("/")
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _fetch_terminal_output(base_url: str, terminal_id: str) -> str:
    try:
        response = requests.get(
            f"{base_url}/terminals/{terminal_id}/output",
            params={"mode": "full"},
            timeout=E2E_REQUEST_TIMEOUT,
        )
        if response.status_code == 200:
            return str(response.json().get("output", ""))
    except Exception:
        pass
    return ""


def _contains_interactive_permissions_prompt(output: str) -> bool:
    output_lc = output.lower()
    return (
        "bypass permissions" in output_lc
        or "shift+tab to cycle" in output_lc
        or "enter to select" in output_lc
    )


def _contains_usage_limit_message(output: str) -> bool:
    output_lc = output.lower()
    return "you've hit your limit" in output_lc or (
        "limit" in output_lc and "resets" in output_lc
    )


def _contains_assistant_token(output: str, token: str) -> bool:
    normalized = ANSI_ESCAPE_PATTERN.sub("", output)
    token_lc = token.lower()
    for line in normalized.splitlines():
        line_lc = line.lower()
        if token_lc in line_lc and "reply exactly with" not in line_lc:
            return True
    return False


def _run_claude_non_interactive_preflight(base_url: str) -> None:
    session_name = f"cao-e2e-preflight-{uuid.uuid4().hex[:8]}"
    terminal_id: str | None = None
    blocked_reason: str | None = None
    expected_token = "E2E_PREFLIGHT_OK"

    try:
        try:
            create = requests.post(
                f"{base_url}/sessions",
                params={
                    "provider": E2E_PROVIDER,
                    "agent_profile": E2E_PROFILE,
                    "session_name": session_name,
                },
                timeout=E2E_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            hint = ""
            if E2E_PROVIDER == "claude_code" and "timed out" in str(exc).lower():
                hint = (
                    " Hint: Claude session startup can block on interactive permissions. "
                    "Start the CAO API with CAO_CLAUDE_PERMISSION_MODE=bypassPermissions "
                    "(or another non-interactive mode)."
                )
            pytest.fail(
                "E2E preflight failed: session creation request failed "
                f"(base_url={base_url}, error={exc}).{hint}"
            )
        if create.status_code != 201:
            pytest.fail(
                "E2E preflight failed: unable to create probe terminal "
                f"(status={create.status_code}, base_url={base_url})"
            )
        terminal_id = create.json()["id"]

        try:
            sent = requests.post(
                f"{base_url}/terminals/{terminal_id}/input",
                params={"message": f"Reply exactly with: {expected_token}"},
                timeout=E2E_REQUEST_TIMEOUT,
            )
        except Exception as exc:
            pytest.fail(
                "E2E preflight failed: probe input request failed "
                f"(terminal={terminal_id}, error={exc})"
            )
        if sent.status_code != 200:
            pytest.fail(
                "E2E preflight failed: unable to send probe input "
                f"(status={sent.status_code}, terminal={terminal_id})"
            )

        deadline = time.time() + E2E_PREFLIGHT_TIMEOUT
        last_status = "unknown"
        while time.time() < deadline:
            try:
                response = requests.get(
                    f"{base_url}/terminals/{terminal_id}", timeout=E2E_REQUEST_TIMEOUT
                )
                if response.status_code == 200:
                    last_status = str(response.json().get("status", "unknown"))
                    if last_status in {"completed", "idle"}:
                        output = _fetch_terminal_output(base_url, terminal_id)
                        if _contains_assistant_token(output, expected_token):
                            return

                    if last_status == "waiting_user_answer":
                        output = _fetch_terminal_output(base_url, terminal_id)
                        blocked_reason = (
                            "E2E preflight failed: Claude provider entered interactive "
                            "permission mode (status=waiting_user_answer). "
                            "Configure non-interactive permissions for CI/E2E."
                        )
                        if output:
                            blocked_reason += f" Terminal output: {output[:220]!r}"
                        break

                    if last_status == "error":
                        output = _fetch_terminal_output(base_url, terminal_id)
                        if _contains_interactive_permissions_prompt(output):
                            blocked_reason = (
                                "E2E preflight failed: Claude provider is waiting on "
                                "interactive permissions prompt. Configure non-interactive "
                                "permissions for CI/E2E."
                            )
                            break
            except Exception:
                pass
            time.sleep(1)

        if blocked_reason:
            pytest.fail(blocked_reason)

        output = _fetch_terminal_output(base_url, terminal_id) if terminal_id else ""
        if _contains_usage_limit_message(output):
            pytest.fail(
                "E2E preflight failed: Claude account/tooling reported a usage limit "
                f"(terminal={terminal_id}). Terminal output: {output[:220]!r}"
            )

        idle_hint = ""
        if last_status == "idle":
            idle_hint = (
                " Terminal stayed idle after input. Claude may not be executing prompts "
                "(for example usage limit, auth/session issue, or interactive composer state)."
            )

        output_hint = f" Terminal output: {output[:220]!r}" if output else ""
        pytest.fail(
            "E2E preflight timed out waiting for Claude probe completion "
            f"(terminal={terminal_id}, last_status={last_status}, timeout={E2E_PREFLIGHT_TIMEOUT}s)."
            f"{idle_hint}{output_hint}"
        )
    finally:
        try:
            requests.delete(
                f"{base_url}/sessions/{session_name}",
                timeout=E2E_REQUEST_TIMEOUT,
            )
        except Exception:
            pass


@pytest.fixture(scope="session")
def cao_server() -> str:
    """Run E2E whenever CAO server is reachable; skip only when unavailable."""
    # Keep MCP/tooling URL resolution aligned with the E2E target URL.
    os.environ["CAO_API_BASE_URL"] = E2E_API_BASE_URL

    last_error: str | None = None
    for _ in range(E2E_HEALTH_ATTEMPTS):
        try:
            response = requests.get(f"{E2E_API_BASE_URL}/health", timeout=E2E_HEALTH_TIMEOUT)
            if response.status_code == 200:
                if E2E_PROVIDER == "claude_code" and E2E_PREFLIGHT_ENABLED:
                    _run_claude_non_interactive_preflight(E2E_API_BASE_URL)
                return E2E_API_BASE_URL
            last_error = f"health status={response.status_code}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(E2E_HEALTH_INTERVAL_SECONDS)

    pytest.skip(
        "CAO server not reachable at "
        f"{E2E_API_BASE_URL} after {E2E_HEALTH_ATTEMPTS} attempts: {last_error}"
    )


@pytest.fixture
def cleanup_sessions(cao_server: str):
    """Track created sessions and delete them after each test."""
    created: List[str] = []

    def _track(session_name_prefix: str) -> str:
        # Use unique session names to avoid collisions with stale tmux sessions from prior runs.
        session_name = f"{session_name_prefix}-{uuid.uuid4().hex[:8]}"
        created.append(session_name)
        return session_name

    yield _track

    for session_name in reversed(created):
        try:
            requests.delete(
                f"{cao_server}/sessions/{session_name}",
                timeout=E2E_REQUEST_TIMEOUT,
            )
        except Exception:
            pass
