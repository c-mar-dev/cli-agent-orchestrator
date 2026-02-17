"""Tests for profile-based configuration defaults and precedence."""

from __future__ import annotations

import importlib
import json
from typing import Any, Dict

import pytest


def _reload_constants_with_profile(
    tmp_path,
    monkeypatch,
    profile_payload: Dict[str, Any],
    *,
    env_overrides: Dict[str, str] | None = None,
):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile_payload), encoding="utf-8")
    monkeypatch.setenv("CAO_PROFILE_PATH", str(profile_path))

    for env_name in [
        "CAO_DEFAULT_PROVIDER",
        "CAO_DEFAULT_WORKING_DIRECTORY",
        "CAO_STATUS_SOURCE",
        "CAO_JSONL_ROLLOUT_TERMINAL_IDS",
        "CAO_JSONL_ROLLOUT_SESSION_NAMES",
        "CAO_JSONL_GATE_FALLBACK_THRESHOLD",
        "CAO_JSONL_GATE_DISAGREEMENT_THRESHOLD",
        "CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD",
        "CAO_JSONL_TMUX_COMPARISON_ENABLED",
        "CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT",
        "CAO_INBOX_MAX_DELIVERY_ATTEMPTS",
        "CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS",
        "CAO_INBOX_RETRY_BACKOFF_MULTIPLIER",
        "CAO_INBOX_RETRY_BACKOFF_MAX_SECONDS",
        "CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT",
        "CAO_APPROVAL_QUEUE_ENABLED",
        "CAO_APPROVAL_PROMPT_TAIL_LINES",
        "CAO_SINGLE_WRITER_ENFORCED",
        "CAO_SINGLE_WRITER_ALLOW_OVERRIDE",
        "CAO_SINGLE_WRITER_LOCKFILE",
    ]:
        monkeypatch.delenv(env_name, raising=False)

    for name, value in (env_overrides or {}).items():
        monkeypatch.setenv(name, value)

    import cli_agent_orchestrator.utils.profile as profile_module

    profile_module.reset_profile_cache()

    import cli_agent_orchestrator.constants as constants

    importlib.reload(constants)
    return constants, profile_module


def test_profile_value_loading_all_config_families(tmp_path, monkeypatch):
    constants, profile_module = _reload_constants_with_profile(
        tmp_path,
        monkeypatch,
        {
            "defaults": {
                "provider": "codex",
                "working_directory": "/tmp/work-from-profile",
                "status_source": "jsonl",
            },
            "jsonl": {
                "rollout": {
                    "terminal_ids": ["aaaa1111", "bbbb2222"],
                    "session_names": ["cao-rollout"],
                },
                "gates": {
                    "fallback_threshold": 0.15,
                    "disagreement_threshold": 0.02,
                    "watcher_error_threshold": 0.005,
                    "tmux_comparison_enabled": True,
                    "enforce_tmux_disagreement": True,
                },
            },
            "inbox": {
                "max_delivery_attempts": 9,
                "retry": {
                    "base_seconds": 3,
                    "multiplier": 1.5,
                    "max_seconds": 120,
                },
                "requeue_terminal_state_default": True,
            },
            "approvals": {
                "enabled": False,
                "prompt_tail_lines": 40,
            },
            "single_writer": {
                "enforced": False,
                "allow_override": True,
                "lockfile": "/tmp/cao-custom.lock",
            },
        },
    )

    assert constants.DEFAULT_PROVIDER == "codex"
    assert constants.DEFAULT_WORKING_DIRECTORY == "/tmp/work-from-profile"
    assert constants.CAO_STATUS_SOURCE == "jsonl"
    assert constants.CAO_JSONL_ROLLOUT_TERMINAL_IDS == {"aaaa1111", "bbbb2222"}
    assert constants.CAO_JSONL_ROLLOUT_SESSION_NAMES == {"cao-rollout"}
    assert constants.CAO_JSONL_GATE_FALLBACK_THRESHOLD == 0.15
    assert constants.CAO_JSONL_GATE_DISAGREEMENT_THRESHOLD == 0.02
    assert constants.CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD == 0.005
    assert constants.CAO_JSONL_TMUX_COMPARISON_ENABLED is True
    assert constants.CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT is True
    assert constants.CAO_INBOX_MAX_DELIVERY_ATTEMPTS == 9
    assert constants.CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS == 3
    assert constants.CAO_INBOX_RETRY_BACKOFF_MULTIPLIER == 1.5
    assert constants.CAO_INBOX_RETRY_BACKOFF_MAX_SECONDS == 120
    assert constants.CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT is True
    assert constants.CAO_APPROVAL_QUEUE_ENABLED is False
    assert constants.CAO_APPROVAL_PROMPT_TAIL_LINES == 40
    assert constants.CAO_SINGLE_WRITER_ENFORCED is False
    assert constants.CAO_SINGLE_WRITER_ALLOW_OVERRIDE is True
    assert str(constants.CAO_SINGLE_WRITER_LOCKFILE) == "/tmp/cao-custom.lock"

    monkeypatch.delenv("CAO_PROFILE_PATH", raising=False)
    profile_module.reset_profile_cache()
    importlib.reload(constants)


@pytest.mark.parametrize(
    ("env_name", "attr_name", "profile_payload", "env_value", "expected"),
    [
        (
            "CAO_JSONL_GATE_FALLBACK_THRESHOLD",
            "CAO_JSONL_GATE_FALLBACK_THRESHOLD",
            {"jsonl": {"gates": {"fallback_threshold": 0.11}}},
            "0.22",
            0.22,
        ),
        (
            "CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS",
            "CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS",
            {"inbox": {"retry": {"base_seconds": 3}}},
            "7",
            7,
        ),
        (
            "CAO_APPROVAL_QUEUE_ENABLED",
            "CAO_APPROVAL_QUEUE_ENABLED",
            {"approvals": {"enabled": False}},
            "true",
            True,
        ),
        (
            "CAO_SINGLE_WRITER_ENFORCED",
            "CAO_SINGLE_WRITER_ENFORCED",
            {"single_writer": {"enforced": False}},
            "true",
            True,
        ),
        (
            "CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT",
            "CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT",
            {"inbox": {"requeue_terminal_state_default": False}},
            "1",
            True,
        ),
    ],
)
def test_env_overrides_profile_values(
    tmp_path,
    monkeypatch,
    env_name: str,
    attr_name: str,
    profile_payload: Dict[str, Any],
    env_value: str,
    expected: Any,
):
    constants, _profile_module = _reload_constants_with_profile(
        tmp_path,
        monkeypatch,
        profile_payload,
        env_overrides={env_name: env_value},
    )

    assert getattr(constants, attr_name) == expected


def test_fallback_defaults_when_profile_and_env_absent(tmp_path, monkeypatch):
    constants, profile_module = _reload_constants_with_profile(tmp_path, monkeypatch, {})

    assert constants.CAO_JSONL_GATE_FALLBACK_THRESHOLD == 0.05
    assert constants.CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS == 2
    assert constants.CAO_APPROVAL_PROMPT_TAIL_LINES == 25
    assert constants.CAO_SINGLE_WRITER_ALLOW_OVERRIDE is False
    assert constants.CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT is False

    monkeypatch.delenv("CAO_PROFILE_PATH", raising=False)
    profile_module.reset_profile_cache()
    importlib.reload(constants)
