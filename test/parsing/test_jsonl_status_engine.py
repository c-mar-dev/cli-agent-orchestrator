"""Unit tests for JSONL status engine."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.parsing import jsonl_status_engine as engine_mod
from cli_agent_orchestrator.parsing.jsonl_status_engine import JsonlStatusEngine


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for item in entries:
            f.write(json.dumps(item) + "\n")


def _copy_fixture_to(path: Path, fixture_name: str) -> None:
    fixture_path = Path(__file__).parent / "fixtures" / fixture_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(fixture_path.read_text(encoding="utf-8"), encoding="utf-8")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def test_claude_parent_session_mapping_ignores_subagent_logs(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    claude_root = tmp_path / ".claude" / "projects"
    project_dir = claude_root / "-home-user-project"
    session_id = "11111111-2222-3333-4444-555555555555"
    parent_log = project_dir / f"{session_id}.jsonl"
    subagent_log = project_dir / "agent-a123456.jsonl"

    _write_jsonl(
        parent_log,
        [
            {
                "type": "user",
                "timestamp": "2026-02-16T18:00:00.000Z",
                "sessionId": session_id,
                "message": {"role": "user", "content": "hello"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ],
    )
    _write_jsonl(
        subagent_log,
        [
            {
                "type": "assistant",
                "timestamp": "2026-02-16T18:00:02.000Z",
                "sessionId": session_id,
                "message": {"role": "assistant", "content": [{"type": "text", "text": "sub"}]},
            }
        ],
    )

    monkeypatch.setattr(engine_mod, "CAO_JSONL_CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime(2026, 2, 16, 18, 1, 0, tzinfo=timezone.utc),
        },
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(
        ProviderType.CLAUDE_CODE.value,
        "t1",
        "session-x",
        "window-0",
    )

    assert result.deterministic is True
    assert result.session_id == session_id
    assert result.log_path == parent_log
    assert result.status == TerminalStatus.COMPLETED
    assert result.last_message == "done"


def test_codex_waiting_flows_map_to_processing(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    codex_root = tmp_path / ".codex" / "sessions" / "2026" / "02" / "16"
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    log_path = codex_root / f"rollout-2026-02-16T18-00-00-{session_id}.jsonl"

    _write_jsonl(
        log_path,
        [
            {
                "type": "session_meta",
                "timestamp": "2026-02-16T18:00:00.000Z",
                "payload": {
                    "id": session_id,
                    "timestamp": "2026-02-16T18:00:00.000Z",
                    "cwd": "/home/user/project",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "payload": {"type": "task_started"},
            },
            {
                "type": "response_item",
                "timestamp": "2026-02-16T18:00:02.000Z",
                "payload": {
                    "type": "function_call",
                    "name": "request_user_input",
                    "arguments": "{}",
                },
            },
        ],
    )

    monkeypatch.setattr(engine_mod, "CAO_JSONL_CODEX_ROOT", tmp_path / ".codex" / "sessions")
    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime(2026, 2, 16, 18, 1, 0, tzinfo=timezone.utc),
        },
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CODEX.value, "t2", "session-y", "window-1")

    assert result.deterministic is True
    assert result.session_id == session_id
    assert result.status == TerminalStatus.PROCESSING
    assert result.reason_code == "codex_waiting_unsupported"


def test_codex_ambiguous_sessions_falls_back(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    codex_root = tmp_path / ".codex" / "sessions" / "2026" / "02" / "16"
    session_a = "aaaaaaaa-bbbb-cccc-dddd-000000000001"
    session_b = "aaaaaaaa-bbbb-cccc-dddd-000000000002"

    _write_jsonl(
        codex_root / f"rollout-2026-02-16T18-00-00-{session_a}.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-02-16T18:00:00.000Z",
                "payload": {
                    "id": session_a,
                    "timestamp": "2026-02-16T18:00:00.000Z",
                    "cwd": "/home/user/project",
                },
            }
        ],
    )
    _write_jsonl(
        codex_root / f"rollout-2026-02-16T18-00-05-{session_b}.jsonl",
        [
            {
                "type": "session_meta",
                "timestamp": "2026-02-16T18:00:05.000Z",
                "payload": {
                    "id": session_b,
                    "timestamp": "2026-02-16T18:00:05.000Z",
                    "cwd": "/home/user/project",
                },
            }
        ],
    )

    monkeypatch.setattr(engine_mod, "CAO_JSONL_CODEX_ROOT", tmp_path / ".codex" / "sessions")
    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime(2026, 2, 16, 18, 1, 0, tzinfo=timezone.utc),
        },
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CODEX.value, "t3", "session-z", "window-2")

    assert result.deterministic is False
    assert result.reason_code == "codex_ambiguous_sessions"
    assert result.status is None


def test_claude_ambiguous_sessions_can_rematch_using_tmux_user_text(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    claude_root = tmp_path / ".claude" / "projects"
    project_dir = claude_root / "-home-user-project"
    session_a = "aaaaaaaa-2222-3333-4444-555555555555"
    session_b = "bbbbbbbb-2222-3333-4444-555555555555"
    log_a = project_dir / f"{session_a}.jsonl"
    log_b = project_dir / f"{session_b}.jsonl"

    _write_jsonl(
        log_a,
        [
            {
                "type": "user",
                "timestamp": "2026-02-16T18:00:00.000Z",
                "sessionId": session_a,
                "message": {
                    "role": "user",
                    "content": "jsonl-delivery-after-remediation",
                },
            },
            {
                "type": "progress",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "sessionId": session_a,
                "data": {"type": "waiting_for_task"},
            },
        ],
    )
    _write_jsonl(
        log_b,
        [
            {
                "type": "user",
                "timestamp": "2026-02-16T18:00:00.000Z",
                "sessionId": session_b,
                "message": {"role": "user", "content": "some-other-feature"},
            }
        ],
    )

    monkeypatch.setattr(engine_mod, "CAO_JSONL_CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime.now(timezone.utc),
        },
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        engine_mod.tmux_client,
        "get_history",
        lambda _session, _window, tail_lines=None: (
            "Feature slug: jsonl-delivery-after-remediation\n❯ jsonl-delivery-after-remediation"
        ),
    )

    result = engine.get_status(
        ProviderType.CLAUDE_CODE.value,
        "t6",
        "session-x",
        "window-0",
    )

    assert result.deterministic is True
    assert result.session_id == session_a
    assert result.reason_code == "claude_waiting_for_task"
    assert result.status == TerminalStatus.WAITING_USER_ANSWER


def test_claude_ask_user_question_maps_to_waiting(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    log_path = tmp_path / "claude-waiting.jsonl"
    session_id = "11111111-2222-3333-4444-666666666666"
    _write_jsonl(
        log_path,
        [
            {
                "type": "user",
                "timestamp": "2026-02-16T18:00:00.000Z",
                "sessionId": session_id,
                "message": {"role": "user", "content": "feature-a"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Need your answer"},
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "AskUserQuestion",
                            "input": {"questions": []},
                        },
                    ],
                },
            },
        ],
    )

    metadata = {
        "provider": ProviderType.CLAUDE_CODE.value,
        "provider_session_id": session_id,
        "provider_log_path": str(log_path),
    }
    monkeypatch.setattr(engine_mod, "get_terminal_metadata", lambda _terminal_id: metadata)
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CLAUDE_CODE.value, "t7", "sess", "win")

    assert result.deterministic is True
    assert result.status == TerminalStatus.WAITING_USER_ANSWER
    assert result.reason_code == "claude_ask_user_question"


def test_claude_new_user_turn_without_progress_is_processing(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    log_path = tmp_path / "claude-inflight.jsonl"
    session_id = "11111111-2222-3333-4444-777777777777"
    now = datetime.now(timezone.utc)
    _write_jsonl(
        log_path,
        [
            {
                "type": "assistant",
                "timestamp": _iso_utc(now),
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "previous answer"}],
                },
            },
            {
                "type": "user",
                "timestamp": _iso_utc(now + timedelta(seconds=1)),
                "sessionId": session_id,
                "message": {"role": "user", "content": "new question"},
            },
        ],
    )

    metadata = {
        "provider": ProviderType.CLAUDE_CODE.value,
        "provider_session_id": session_id,
        "provider_log_path": str(log_path),
    }
    monkeypatch.setattr(engine_mod, "get_terminal_metadata", lambda _terminal_id: metadata)
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CLAUDE_CODE.value, "t8", "sess", "win")

    assert result.deterministic is True
    assert result.status == TerminalStatus.PROCESSING
    assert result.reason_code == "claude_user_turn_inflight"


def test_claude_agent_progress_assistant_message_maps_to_completed(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    log_path = tmp_path / "claude-agent-progress-completed.jsonl"
    session_id = "11111111-2222-3333-4444-888888888888"
    now = datetime.now(timezone.utc)
    _write_jsonl(
        log_path,
        [
            {
                "type": "user",
                "timestamp": _iso_utc(now),
                "sessionId": session_id,
                "message": {"role": "user", "content": "new prompt"},
            },
            {
                "type": "progress",
                "timestamp": _iso_utc(now),
                "sessionId": session_id,
                "data": {
                    "type": "agent_progress",
                    "message": {
                        "type": "assistant",
                        "timestamp": _iso_utc(now),
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "completed reply"}],
                        },
                    },
                },
            },
        ],
    )

    metadata = {
        "provider": ProviderType.CLAUDE_CODE.value,
        "provider_session_id": session_id,
        "provider_log_path": str(log_path),
    }
    monkeypatch.setattr(engine_mod, "get_terminal_metadata", lambda _terminal_id: metadata)
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CLAUDE_CODE.value, "t9", "sess", "win")

    assert result.deterministic is True
    assert result.status == TerminalStatus.COMPLETED
    assert result.reason_code == "claude_assistant_completed"
    assert result.last_message == "completed reply"


def test_claude_hook_progress_stop_overrides_recent_processing(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    log_path = tmp_path / "claude-hook-stop.jsonl"
    session_id = "11111111-2222-3333-4444-999999999998"
    now = datetime.now(timezone.utc)
    _write_jsonl(
        log_path,
        [
            {
                "type": "user",
                "timestamp": _iso_utc(now),
                "sessionId": session_id,
                "message": {"role": "user", "content": "run it"},
            },
            {
                "type": "progress",
                "timestamp": _iso_utc(now),
                "sessionId": session_id,
                "data": {"type": "agent_progress"},
            },
            {
                "type": "progress",
                "timestamp": _iso_utc(now),
                "sessionId": session_id,
                "data": {"type": "hook_progress", "hookEvent": "Stop", "hookName": "Stop"},
            },
        ],
    )

    metadata = {
        "provider": ProviderType.CLAUDE_CODE.value,
        "provider_session_id": session_id,
        "provider_log_path": str(log_path),
    }
    monkeypatch.setattr(engine_mod, "get_terminal_metadata", lambda _terminal_id: metadata)
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CLAUDE_CODE.value, "t10", "sess", "win")

    assert result.deterministic is True
    assert result.status == TerminalStatus.COMPLETED
    assert result.reason_code == "claude_hook_stop_completed"


def test_incremental_reader_tolerates_schema_drift_and_malformed_lines(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    log_path = tmp_path / "claude-parent.jsonl"
    _copy_fixture_to(log_path, "claude_schema_drift.jsonl")

    metadata = {
        "provider": ProviderType.CLAUDE_CODE.value,
        "provider_session_id": "11111111-2222-3333-4444-555555555555",
        "provider_log_path": str(log_path),
    }
    monkeypatch.setattr(engine_mod, "get_terminal_metadata", lambda _terminal_id: metadata)
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    engine.reset_telemetry()

    first = engine.get_status(ProviderType.CLAUDE_CODE.value, "t4", "sess", "win")
    assert first.deterministic is True
    assert first.status == TerminalStatus.COMPLETED
    assert first.last_message == "done"

    with log_path.open("a", encoding="utf-8") as f:
        f.write("{this-is-not-json}\n")
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "timestamp": "2026-02-16T18:00:02.000Z",
                    "sessionId": "11111111-2222-3333-4444-555555555555",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "second response"}],
                    },
                }
            )
            + "\n"
        )

    second = engine.get_status(ProviderType.CLAUDE_CODE.value, "t4", "sess", "win")
    assert second.deterministic is True
    assert second.status == TerminalStatus.COMPLETED
    assert second.last_message == "second response"

    telemetry = engine.get_telemetry_snapshot()
    assert telemetry["malformed_lines"] >= 1


def test_incremental_reader_detects_truncation_and_resets(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    log_path = tmp_path / "codex-rollout.jsonl"
    _copy_fixture_to(log_path, "codex_schema_drift.jsonl")
    now = datetime.now(timezone.utc)

    metadata = {
        "provider": ProviderType.CODEX.value,
        "provider_session_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "provider_log_path": str(log_path),
    }
    monkeypatch.setattr(engine_mod, "get_terminal_metadata", lambda _terminal_id: metadata)
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    engine.reset_telemetry()
    first = engine.get_status(ProviderType.CODEX.value, "t5", "sess", "win")
    assert first.deterministic is True
    assert first.status == TerminalStatus.COMPLETED

    _write_jsonl(
        log_path,
        [
            {
                "type": "session_meta",
                "timestamp": _iso_utc(now),
                "payload": {
                    "id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                    "timestamp": _iso_utc(now),
                    "cwd": "/home/user/project",
                },
            },
            {
                "type": "event_msg",
                "timestamp": _iso_utc(now),
                "payload": {"type": "task_started"},
            },
        ],
    )
    second = engine.get_status(ProviderType.CODEX.value, "t5", "sess", "win")
    assert second.deterministic is True
    assert second.status == TerminalStatus.PROCESSING

    telemetry = engine.get_telemetry_snapshot()
    assert telemetry["parser_resets"] >= 1


def test_persisted_mapping_stays_sticky_on_tmux_text_mismatch(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    session_id = "11111111-2222-3333-4444-999999999999"
    log_path = tmp_path / f"{session_id}.jsonl"
    _write_jsonl(
        log_path,
        [
            {
                "type": "user",
                "timestamp": "2026-02-16T18:00:00.000Z",
                "sessionId": session_id,
                "message": {"role": "user", "content": "persisted command"},
            },
            {
                "type": "assistant",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "sessionId": session_id,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            },
        ],
    )

    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime(2026, 2, 16, 18, 0, 0, tzinfo=timezone.utc),
            "provider_session_id": session_id,
            "provider_log_path": str(log_path),
            "status_source": "jsonl",
        },
    )
    monkeypatch.setattr(
        engine_mod.tmux_client,
        "get_history",
        lambda _session, _window, tail_lines=None: "❯ a completely different snippet",
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CLAUDE_CODE.value, "sticky1", "sess", "win")

    assert result.deterministic is True
    assert result.session_id == session_id
    assert result.log_path == log_path
    assert result.status == TerminalStatus.COMPLETED
    telemetry = engine.get_telemetry_snapshot()
    assert telemetry["mapping_demotions"] == 0


def test_missing_persisted_log_records_invalidation(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    claude_root = tmp_path / ".claude" / "projects"
    project_dir = claude_root / "-home-user-project"
    session_a = "aaaaaaaa-2222-3333-4444-555555555555"
    session_b = "bbbbbbbb-2222-3333-4444-555555555555"
    _write_jsonl(
        project_dir / f"{session_a}.jsonl",
        [{"type": "user", "timestamp": "2026-02-16T18:00:00.000Z", "message": {"role": "user"}}],
    )
    _write_jsonl(
        project_dir / f"{session_b}.jsonl",
        [{"type": "user", "timestamp": "2026-02-16T18:00:00.000Z", "message": {"role": "user"}}],
    )

    monkeypatch.setattr(engine_mod, "CAO_JSONL_CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime(2026, 2, 16, 18, 0, 0, tzinfo=timezone.utc),
            "provider_session_id": "cccccccc-2222-3333-4444-555555555555",
            "provider_log_path": str(project_dir / "cccccccc-2222-3333-4444-555555555555.jsonl"),
        },
    )
    monkeypatch.setattr(
        engine_mod.tmux_client,
        "get_history",
        lambda _session, _window, tail_lines=None: "",
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CLAUDE_CODE.value, "sticky2", "sess", "win")

    assert result.deterministic is False
    assert result.reason_code == "claude_ambiguous_parent_sessions"
    telemetry = engine.get_telemetry_snapshot()
    assert telemetry["mapping_invalidations"] >= 1
    assert telemetry["mapping_invalidation_reason_counts"]["persisted_log_path_missing"] >= 1


def test_claude_mapping_uses_provider_session_hint(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    claude_root = tmp_path / ".claude" / "projects"
    project_dir = claude_root / "-home-user-project"
    hinted_session_id = "aaaaaaaa-2222-3333-4444-555555555555"
    other_session_id = "bbbbbbbb-2222-3333-4444-555555555555"
    hinted_path = project_dir / f"{hinted_session_id}.jsonl"
    other_path = project_dir / f"{other_session_id}.jsonl"

    _write_jsonl(
        hinted_path,
        [
            {
                "type": "assistant",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "sessionId": hinted_session_id,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                },
            }
        ],
    )
    _write_jsonl(
        other_path,
        [
            {
                "type": "assistant",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "sessionId": other_session_id,
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "other"}],
                },
            }
        ],
    )

    monkeypatch.setattr(engine_mod, "CAO_JSONL_CLAUDE_ROOT", claude_root)
    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime(2026, 2, 16, 18, 0, 0, tzinfo=timezone.utc),
            "provider_session_id": hinted_session_id,
        },
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CLAUDE_CODE.value, "hinted1", "sess", "win")

    assert result.deterministic is True
    assert result.session_id == hinted_session_id
    assert result.log_path == hinted_path
    assert result.status == TerminalStatus.COMPLETED
    assert result.last_message == "done"


def test_codex_index_parses_session_meta_when_not_first_line(monkeypatch, tmp_path):
    engine = JsonlStatusEngine()
    codex_root = tmp_path / ".codex" / "sessions" / "2026" / "02" / "16"
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    log_path = codex_root / f"rollout-2026-02-16T18-00-00-{session_id}.jsonl"

    _write_jsonl(
        log_path,
        [
            {"type": "event_msg", "timestamp": "2026-02-16T18:00:00.000Z", "payload": {"x": 1}},
            {
                "type": "session_meta",
                "timestamp": "2026-02-16T18:00:00.500Z",
                "payload": {
                    "id": session_id,
                    "timestamp": "2026-02-16T18:00:00.500Z",
                    "cwd": "/home/user/project",
                },
            },
            {
                "type": "response_item",
                "timestamp": "2026-02-16T18:00:01.000Z",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "done"}],
                },
            },
        ],
    )

    monkeypatch.setattr(engine_mod, "CAO_JSONL_CODEX_ROOT", tmp_path / ".codex" / "sessions")
    monkeypatch.setattr(
        engine_mod,
        "get_terminal_metadata",
        lambda _terminal_id: {
            "launch_cwd": "/home/user/project",
            "created_at": datetime(2026, 2, 16, 18, 0, 0, tzinfo=timezone.utc),
        },
    )
    monkeypatch.setattr(engine_mod, "update_terminal_mapping", lambda *args, **kwargs: True)

    result = engine.get_status(ProviderType.CODEX.value, "hinted2", "sess", "win")

    assert result.deterministic is True
    assert result.session_id == session_id
    assert result.log_path == log_path
