"""Provider lifecycle coverage for remaining Claude Code gaps."""

from __future__ import annotations

import json
import shlex

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider


def test_claude_build_command_includes_system_prompt_and_mcp_servers(monkeypatch):
    profile = AgentProfile(
        name="developer",
        description="dev profile",
        system_prompt="line one\nline two",
        mcpServers={"local": {"command": "npx", "args": ["-y", "my-mcp"]}},
    )

    monkeypatch.setattr("cli_agent_orchestrator.providers.claude_code.load_agent_profile", lambda _name: profile)

    provider = ClaudeCodeProvider("abcd1234", "cao-main", "win-1", agent_profile="developer")
    command = provider._build_claude_command()
    parts = shlex.split(command)

    assert parts[0] == "claude"
    assert "--append-system-prompt" in parts
    assert "line one\\nline two" in parts

    mcp_index = parts.index("--mcp-config")
    mcp_payload = json.loads(parts[mcp_index + 1])
    assert "mcpServers" in mcp_payload
    assert "local" in mcp_payload["mcpServers"]


def test_claude_initialize_sends_unset_claudecode_and_command(monkeypatch):
    sent = []

    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.tmux_client.send_keys",
        lambda session, window, command: sent.append((session, window, command)),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.claude_code.wait_until_status",
        lambda *_args, **_kwargs: True,
    )

    provider = ClaudeCodeProvider("abcd1234", "cao-main", "win-1", agent_profile="developer")
    monkeypatch.setattr(provider, "_build_claude_command", lambda: "claude --model sonnet")

    assert provider.initialize() is True
    assert provider._initialized is True
    assert sent == [("cao-main", "win-1", "unset CLAUDECODE && claude --model sonnet")]


def test_claude_build_command_includes_permission_mode_env(monkeypatch):
    monkeypatch.setenv("CAO_CLAUDE_PERMISSION_MODE", "bypassPermissions")
    monkeypatch.delenv("CAO_CLAUDE_DANGEROUS_SKIP_PERMISSIONS", raising=False)
    monkeypatch.delenv("CAO_CLAUDE_ALLOW_DANGEROUS_SKIP_PERMISSIONS", raising=False)

    provider = ClaudeCodeProvider("abcd1234", "cao-main", "win-1")
    parts = shlex.split(provider._build_claude_command())

    assert "--permission-mode" in parts
    assert "bypassPermissions" in parts


def test_claude_build_command_includes_dangerous_skip_permissions_flag(monkeypatch):
    monkeypatch.delenv("CAO_CLAUDE_PERMISSION_MODE", raising=False)
    monkeypatch.setenv("CAO_CLAUDE_DANGEROUS_SKIP_PERMISSIONS", "true")
    monkeypatch.delenv("CAO_CLAUDE_ALLOW_DANGEROUS_SKIP_PERMISSIONS", raising=False)

    provider = ClaudeCodeProvider("abcd1234", "cao-main", "win-1")
    parts = shlex.split(provider._build_claude_command())

    assert "--dangerously-skip-permissions" in parts
    assert "--allow-dangerously-skip-permissions" not in parts
