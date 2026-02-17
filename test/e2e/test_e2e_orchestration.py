"""Manual E2E tests for real tmux + real CLI agents."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
import requests

from cli_agent_orchestrator.mcp_server import server


E2E_PROVIDER = os.getenv("CAO_E2E_PROVIDER", "claude_code")
E2E_PROFILE = os.getenv("CAO_E2E_PROFILE", "developer")
API_REQUEST_TIMEOUT = float(os.getenv("CAO_E2E_REQUEST_TIMEOUT", "45"))
E2E_HANDOFF_TIMEOUT = float(os.getenv("CAO_E2E_HANDOFF_TIMEOUT", "180"))
E2E_ASSIGN_COMPLETION_TIMEOUT = float(os.getenv("CAO_E2E_ASSIGN_COMPLETION_TIMEOUT", "240"))


def _get_terminal_status(base_url: str, terminal_id: str) -> str:
    try:
        response = requests.get(f"{base_url}/terminals/{terminal_id}", timeout=5)
        if response.status_code == 200:
            return str(response.json().get("status", "unknown"))
    except Exception:
        pass
    return "unknown"


def _wait_for_terminal_status(base_url: str, terminal_id: str, status: str, timeout: float = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = requests.get(f"{base_url}/terminals/{terminal_id}", timeout=5)
            if response.status_code == 200:
                current_status = response.json().get("status")
                if current_status == status:
                    return True
                if current_status == "waiting_user_answer":
                    return False
        except Exception:
            pass
        time.sleep(1)
    return False


@pytest.mark.e2e
def test_e2e_handoff_with_claude_code(cao_server: str, cleanup_sessions):
    session_name = cleanup_sessions("cao-e2e-handoff")
    create = requests.post(
        f"{cao_server}/sessions",
        params={"provider": E2E_PROVIDER, "agent_profile": E2E_PROFILE, "session_name": session_name},
        timeout=API_REQUEST_TIMEOUT,
    )
    assert create.status_code == 201
    conductor_id = create.json()["id"]

    os.environ["CAO_TERMINAL_ID"] = conductor_id
    result = asyncio.run(server._handoff_impl(E2E_PROFILE, "Reply with: E2E_OK", timeout=E2E_HANDOFF_TIMEOUT))

    assert result.success is True, result.message
    assert result.output is not None


@pytest.mark.e2e
def test_e2e_assign_and_poll_until_completed(cao_server: str, cleanup_sessions):
    session_name = cleanup_sessions("cao-e2e-assign")
    create = requests.post(
        f"{cao_server}/sessions",
        params={"provider": E2E_PROVIDER, "agent_profile": E2E_PROFILE, "session_name": session_name},
        timeout=API_REQUEST_TIMEOUT,
    )
    assert create.status_code == 201
    conductor_id = create.json()["id"]

    os.environ["CAO_TERMINAL_ID"] = conductor_id
    assigned = server._assign_impl(E2E_PROFILE, "Reply exactly with: E2E_ASSIGN_OK")

    assert assigned["success"] is True
    worker_id = assigned["terminal_id"]
    assert worker_id is not None

    completed = _wait_for_terminal_status(
        cao_server,
        worker_id,
        "completed",
        timeout=E2E_ASSIGN_COMPLETION_TIMEOUT,
    )
    assert completed, (
        f"Worker terminal {worker_id} did not complete within {E2E_ASSIGN_COMPLETION_TIMEOUT}s "
        f"(final_status={_get_terminal_status(cao_server, worker_id)})"
    )


@pytest.mark.e2e
def test_e2e_send_message_between_terminals(cao_server: str, cleanup_sessions):
    session_name = cleanup_sessions("cao-e2e-message")
    first = requests.post(
        f"{cao_server}/sessions",
        params={"provider": E2E_PROVIDER, "agent_profile": E2E_PROFILE, "session_name": session_name},
        timeout=API_REQUEST_TIMEOUT,
    )
    assert first.status_code == 201
    sender_id = first.json()["id"]

    second = requests.post(
        f"{cao_server}/sessions/{session_name}/terminals",
        params={"provider": E2E_PROVIDER, "agent_profile": E2E_PROFILE},
        timeout=API_REQUEST_TIMEOUT,
    )
    assert second.status_code == 201
    receiver_id = second.json()["id"]

    os.environ["CAO_TERMINAL_ID"] = sender_id
    sent = server._send_to_inbox(receiver_id, "E2E inbox ping")

    assert sent.get("success") is True


@pytest.mark.e2e
def test_e2e_session_lifecycle(cao_server: str, cleanup_sessions):
    session_name = cleanup_sessions("cao-e2e-lifecycle")
    created = requests.post(
        f"{cao_server}/sessions",
        params={"provider": E2E_PROVIDER, "agent_profile": E2E_PROFILE, "session_name": session_name},
        timeout=API_REQUEST_TIMEOUT,
    )
    assert created.status_code == 201

    added = requests.post(
        f"{cao_server}/sessions/{session_name}/terminals",
        params={"provider": E2E_PROVIDER, "agent_profile": E2E_PROFILE},
        timeout=API_REQUEST_TIMEOUT,
    )
    assert added.status_code == 201

    listed = requests.get(f"{cao_server}/sessions/{session_name}", timeout=API_REQUEST_TIMEOUT)
    assert listed.status_code == 200
    assert len(listed.json().get("terminals", [])) >= 2

    deleted = requests.delete(f"{cao_server}/sessions/{session_name}", timeout=API_REQUEST_TIMEOUT)
    assert deleted.status_code == 200


@pytest.mark.e2e
def test_e2e_working_directory(cao_server: str, cleanup_sessions, tmp_path):
    session_name = cleanup_sessions("cao-e2e-cwd")
    workdir = str(Path(tmp_path).resolve())

    created = requests.post(
        f"{cao_server}/sessions",
        params={
            "provider": E2E_PROVIDER,
            "agent_profile": E2E_PROFILE,
            "session_name": session_name,
            "working_directory": workdir,
        },
        timeout=API_REQUEST_TIMEOUT,
    )
    assert created.status_code == 201
    terminal_id = created.json()["id"]

    cwd = requests.get(
        f"{cao_server}/terminals/{terminal_id}/working-directory", timeout=API_REQUEST_TIMEOUT
    )
    assert cwd.status_code == 200
    assert cwd.json().get("working_directory") == workdir
