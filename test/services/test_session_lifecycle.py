"""Integration tests for session lifecycle behavior."""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.approval import ApprovalStatus
from cli_agent_orchestrator.services import session_service, terminal_service


def test_create_session_creates_tmux_and_db_record(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="main-session",
        new_session=True,
    )

    assert fake_tmux.session_exists("cao-main-session") is True
    metadata = database.get_terminal_metadata(terminal.id)
    assert metadata is not None
    assert metadata["tmux_session"] == "cao-main-session"


def test_list_sessions_filters_by_prefix(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    fake_tmux.create_session("cao-a", "w1", "t1")
    fake_tmux.create_session("random", "w2", "t2")

    sessions = session_service.list_sessions()
    assert [s["id"] for s in sessions] == ["cao-a"]


def test_get_session_returns_terminals(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    first = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="alpha",
        new_session=True,
    )
    second = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="reviewer",
        session_name="cao-alpha",
        new_session=False,
    )

    payload = session_service.get_session("cao-alpha")
    assert payload["session"]["id"] == "cao-alpha"
    assert sorted(t["id"] for t in payload["terminals"]) == sorted([first.id, second.id])


def test_delete_session_cleans_tmux_db_and_providers(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    t1 = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="delete-me",
        new_session=True,
    )
    t2 = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="reviewer",
        session_name="cao-delete-me",
        new_session=False,
    )

    assert t1.id in fake_provider_manager._providers
    assert t2.id in fake_provider_manager._providers

    assert session_service.delete_session("cao-delete-me") is True

    assert fake_tmux.session_exists("cao-delete-me") is False
    assert database.list_terminals_by_session("cao-delete-me") == []
    assert t1.id not in fake_provider_manager._providers
    assert t2.id not in fake_provider_manager._providers


def test_delete_nonexistent_session_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    with pytest.raises(ValueError, match="not found"):
        session_service.delete_session("cao-missing")


def test_delete_session_cleans_db_even_if_kill_returns_false(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
    monkeypatch,
):
    terminal = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="kill-false",
        new_session=True,
    )

    monkeypatch.setattr(fake_tmux, "kill_session", lambda _name: False)

    assert session_service.delete_session("cao-kill-false") is True
    assert database.get_terminal_metadata(terminal.id) is None
    assert terminal.id not in fake_provider_manager._providers


def test_delete_session_resolves_pending_approvals_for_all_terminals(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    t1 = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="approvals-session",
        new_session=True,
    )
    t2 = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="reviewer",
        session_name="cao-approvals-session",
        new_session=False,
    )
    database.create_or_get_pending_approval(
        t1.id,
        provider="q_cli",
        status_reason_code="waiting",
        prompt_excerpt="approve 1",
        source="status",
    )
    database.create_or_get_pending_approval(
        t2.id,
        provider="q_cli",
        status_reason_code="waiting",
        prompt_excerpt="approve 2",
        source="status",
    )

    assert session_service.delete_session("cao-approvals-session") is True

    for terminal_id in (t1.id, t2.id):
        pending = database.list_approval_requests(terminal_id, status=ApprovalStatus.PENDING)
        resolved = database.list_approval_requests(terminal_id, status=ApprovalStatus.RESOLVED)
        assert pending == []
        assert len(resolved) == 1
        assert resolved[0].resolution_message == "terminal-deleted"


def test_add_terminal_to_existing_session(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    first = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="developer",
        session_name="add-terminal",
        new_session=True,
    )

    second = terminal_service.create_terminal(
        provider="q_cli",
        agent_profile="reviewer",
        session_name="cao-add-terminal",
        new_session=False,
    )

    terminals = database.list_terminals_by_session("cao-add-terminal")
    assert sorted(t["id"] for t in terminals) == sorted([first.id, second.id])


def test_add_terminal_to_nonexistent_session_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    disable_jsonl,
    terminal_log_dir,
):
    with pytest.raises(ValueError, match="not found"):
        terminal_service.create_terminal(
            provider="q_cli",
            agent_profile="developer",
            session_name="cao-nope",
            new_session=False,
        )
