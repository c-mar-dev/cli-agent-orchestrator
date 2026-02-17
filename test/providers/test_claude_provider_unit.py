"""Unit tests for Claude provider tmux-only status parsing."""

import re

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


def test_claude_tmux_ready_prompt_wins_over_stale_processing_glyph(monkeypatch):
    provider = ClaudeCodeProvider("t1", "s1", "w1")
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.tmux_client.get_history",
        lambda *_args, **_kwargs: "✻ Thinking...\n❯ ",
    )

    status = provider._get_status_from_tmux(tail_lines=5)
    assert status == TerminalStatus.IDLE


def test_claude_tmux_waiting_requires_navigation_hint(monkeypatch):
    provider = ClaudeCodeProvider("t1", "s1", "w1")
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.tmux_client.get_history",
        lambda *_args, **_kwargs: "❯ 1. I'll describe it\nEnter to select · ↑/↓ to navigate",
    )
    assert provider._get_status_from_tmux(tail_lines=5) == TerminalStatus.WAITING_USER_ANSWER

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.tmux_client.get_history",
        lambda *_args, **_kwargs: "❯ 1. I'll describe it\n❯ ",
    )
    assert provider._get_status_from_tmux(tail_lines=5) == TerminalStatus.IDLE


def test_claude_tmux_processing_when_no_ready_prompt(monkeypatch):
    provider = ClaudeCodeProvider("t1", "s1", "w1")
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.tmux_client.get_history",
        lambda *_args, **_kwargs: "✻ working…",
    )

    status = provider._get_status_from_tmux(tail_lines=5)
    assert status == TerminalStatus.PROCESSING


def test_claude_tmux_bypass_permissions_hint_without_selection_prompt_is_idle(monkeypatch):
    provider = ClaudeCodeProvider("t1", "s1", "w1")
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.tmux_client.get_history",
        lambda *_args, **_kwargs: (
            '❯ Try "write a test for tmux.py"\n'
            "Say hello and finish.\n"
            "⏵⏵ bypass permissions on (shift+tab to cycle)\n"
            "ctrl+g to edit in VS Code\n"
        ),
    )

    assert provider._get_status_from_tmux(tail_lines=20) == TerminalStatus.IDLE


def test_claude_tmux_bypass_permissions_prompt_with_selection_hint_is_waiting(monkeypatch):
    provider = ClaudeCodeProvider("t1", "s1", "w1")
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.tmux_client.get_history",
        lambda *_args, **_kwargs: (
            "⏵⏵ bypass permissions on (shift+tab to cycle)\n"
            "Enter to select · ↑/↓ to navigate\n"
        ),
    )

    assert provider._get_status_from_tmux(tail_lines=20) == TerminalStatus.WAITING_USER_ANSWER


def test_claude_command_includes_deterministic_session_id():
    provider = ClaudeCodeProvider("t1", "s1", "w1")
    command = provider._build_claude_command()

    assert "--session-id" in command
    assert re.search(
        r"--session-id\s+[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        command,
    )
    assert provider.get_provider_session_hint() is not None
