"""Unit tests for inbox service JSONL watcher and telemetry behavior."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import inbox_service


def test_check_and_send_pending_messages_records_disagreement(monkeypatch):
    inbox_service.reset_inbox_telemetry()
    monkeypatch.setattr(inbox_service, "CAO_JSONL_TMUX_COMPARISON_ENABLED", True)

    msg = Mock(id=42, message="hello")
    monkeypatch.setattr(inbox_service, "get_pending_messages", lambda _tid, limit=1: [msg])

    provider = Mock()
    provider.get_status.return_value = TerminalStatus.IDLE
    provider.get_tmux_status.return_value = TerminalStatus.PROCESSING
    provider.uses_jsonl_status.return_value = True
    monkeypatch.setattr(inbox_service.provider_manager, "get_provider", lambda _tid: provider)

    monkeypatch.setattr(
        inbox_service,
        "get_terminal_metadata",
        lambda _tid: {"status_source": "jsonl"},
    )

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        inbox_service.terminal_service,
        "send_input",
        lambda terminal_id, message: sent.append((terminal_id, message)) or True,
    )

    status_updates: list[int] = []
    monkeypatch.setattr(
        inbox_service,
        "mark_message_delivered",
        lambda mid: status_updates.append(mid) or True,
    )

    delivered = inbox_service.check_and_send_pending_messages("abcd1234", trigger_source="jsonl")
    assert delivered is True
    assert sent == [("abcd1234", "hello")]
    assert status_updates == [42]

    telemetry = inbox_service.get_inbox_telemetry_snapshot()
    assert telemetry["jsonl_tmux_comparisons"] == 1
    assert telemetry["jsonl_tmux_disagreements"] == 1
    assert telemetry["deliveries_succeeded"] == 1


def test_check_and_send_pending_messages_treats_ready_states_as_equivalent(monkeypatch):
    inbox_service.reset_inbox_telemetry()
    monkeypatch.setattr(inbox_service, "CAO_JSONL_TMUX_COMPARISON_ENABLED", True)

    msg = Mock(id=43, message="hello")
    monkeypatch.setattr(inbox_service, "get_pending_messages", lambda _tid, limit=1: [msg])

    provider = Mock()
    provider.get_status.return_value = TerminalStatus.COMPLETED
    provider.get_tmux_status.return_value = TerminalStatus.IDLE
    provider.uses_jsonl_status.return_value = True
    monkeypatch.setattr(inbox_service.provider_manager, "get_provider", lambda _tid: provider)

    monkeypatch.setattr(
        inbox_service,
        "get_terminal_metadata",
        lambda _tid: {"status_source": "jsonl"},
    )

    monkeypatch.setattr(
        inbox_service.terminal_service,
        "send_input",
        lambda _terminal_id, _message: True,
    )
    monkeypatch.setattr(inbox_service, "mark_message_delivered", lambda *_args, **_kwargs: True)

    delivered = inbox_service.check_and_send_pending_messages("abcd1234", trigger_source="jsonl")
    assert delivered is True

    telemetry = inbox_service.get_inbox_telemetry_snapshot()
    assert telemetry["jsonl_tmux_comparisons"] == 1
    assert telemetry["jsonl_tmux_disagreements"] == 0


def test_check_and_send_pending_messages_delivers_on_waiting_user_answer(monkeypatch):
    inbox_service.reset_inbox_telemetry()
    monkeypatch.setattr(inbox_service, "CAO_JSONL_TMUX_COMPARISON_ENABLED", True)

    msg = Mock(id=44, message="select option 2")
    monkeypatch.setattr(inbox_service, "get_pending_messages", lambda _tid, limit=1: [msg])

    provider = Mock()
    provider.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
    provider.get_tmux_status.return_value = TerminalStatus.COMPLETED
    provider.uses_jsonl_status.return_value = True
    monkeypatch.setattr(inbox_service.provider_manager, "get_provider", lambda _tid: provider)

    monkeypatch.setattr(
        inbox_service,
        "get_terminal_metadata",
        lambda _tid: {"status_source": "jsonl"},
    )

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        inbox_service.terminal_service,
        "send_input",
        lambda terminal_id, message: sent.append((terminal_id, message)) or True,
    )

    status_updates: list[int] = []
    monkeypatch.setattr(
        inbox_service,
        "mark_message_delivered",
        lambda mid: status_updates.append(mid) or True,
    )

    delivered = inbox_service.check_and_send_pending_messages("abcd1234", trigger_source="jsonl")
    assert delivered is True
    assert sent == [("abcd1234", "select option 2")]
    assert status_updates == [44]

    telemetry = inbox_service.get_inbox_telemetry_snapshot()
    assert telemetry["jsonl_tmux_comparisons"] == 1
    assert telemetry["jsonl_tmux_disagreements"] == 0
    assert telemetry["deliveries_succeeded"] == 1


def test_check_and_send_pending_messages_fails_orphan_terminal_messages(monkeypatch):
    inbox_service.reset_inbox_telemetry()

    batch1 = [Mock(id=101), Mock(id=102)]
    state = {"calls": 0}

    def _pending(_tid, limit=1):
        state["calls"] += 1
        if state["calls"] == 1:
            return batch1
        return []

    updates: list[int] = []
    monkeypatch.setattr(inbox_service, "get_terminal_metadata", lambda _tid: None)
    monkeypatch.setattr(inbox_service, "get_pending_messages", _pending)
    monkeypatch.setattr(
        inbox_service,
        "mark_message_dead_letter",
        lambda mid, _reason: updates.append(mid) or True,
    )

    # Should short-circuit before provider lookup for orphan receivers.
    monkeypatch.setattr(
        inbox_service.provider_manager,
        "get_provider",
        lambda _tid: (_ for _ in ()).throw(AssertionError("provider lookup should not happen")),
    )

    delivered = inbox_service.check_and_send_pending_messages("orphan-terminal", trigger_source="poll")

    assert delivered is False
    assert updates == [101, 102]


def test_jsonl_handler_routes_events_to_matching_terminals(monkeypatch):
    inbox_service.reset_inbox_telemetry()
    handler = inbox_service.JsonlFileHandler()
    monkeypatch.setattr(
        inbox_service,
        "_resolve_terminals_for_jsonl_path",
        lambda _path: ["abcd1234", "beef5678"],
    )

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        inbox_service,
        "check_and_send_pending_messages",
        lambda terminal_id, trigger_source="manual": calls.append((terminal_id, trigger_source)),
    )

    handler._handle_jsonl_change(Path("/tmp/rollout-test.jsonl"))

    assert calls == [
        ("abcd1234", "jsonl"),
        ("beef5678", "jsonl"),
    ]
    telemetry = inbox_service.get_inbox_telemetry_snapshot()
    assert telemetry["jsonl_watcher_events_received"] == 1
    assert telemetry["last_jsonl_event_path"] == "/tmp/rollout-test.jsonl"


def test_configure_watcher_telemetry_reflected_in_snapshot():
    inbox_service.reset_inbox_telemetry()
    inbox_service.configure_watcher_telemetry(
        jsonl_watch_enabled=True,
        jsonl_watch_paths=["/tmp/jsonl-a", "/tmp/jsonl-b"],
        observer_started=True,
    )
    telemetry = inbox_service.get_inbox_telemetry_snapshot()
    assert telemetry["jsonl_watch_enabled"] is True
    assert telemetry["jsonl_watcher_active"] is True
    assert telemetry["jsonl_watch_paths"] == ["/tmp/jsonl-a", "/tmp/jsonl-b"]
    assert telemetry["jsonl_watcher_started_at"] is not None


def test_reset_jsonl_canary_state_preserves_watcher_config():
    inbox_service.reset_inbox_telemetry()
    inbox_service.configure_watcher_telemetry(
        jsonl_watch_enabled=True,
        jsonl_watch_paths=["/tmp/jsonl-a", "/tmp/jsonl-b"],
        observer_started=True,
    )

    result = inbox_service.reset_jsonl_canary_state(reset_parser=False)
    telemetry = inbox_service.get_inbox_telemetry_snapshot()

    assert result["inbox_telemetry_reset"] is True
    assert result["parser_telemetry_reset"] is False
    assert telemetry["jsonl_watch_enabled"] is True
    assert telemetry["jsonl_watcher_active"] is True
    assert telemetry["jsonl_watch_paths"] == ["/tmp/jsonl-a", "/tmp/jsonl-b"]


def test_canary_gate_fails_with_unmapped_terminals(monkeypatch):
    monkeypatch.setattr(
        inbox_service,
        "get_inbox_telemetry_snapshot",
        lambda: {
            "rates": {
                "jsonl_vs_tmux_disagreement_rate": 0.0,
                "fallback_trigger_rate": 0.0,
                "watcher_error_rate": 0.0,
            }
        },
    )
    monkeypatch.setattr(
        inbox_service.jsonl_status_engine,
        "get_telemetry_snapshot",
        lambda: {"lines_seen": 10, "malformed_lines": 0},
    )
    monkeypatch.setattr(
        inbox_service,
        "list_terminals",
        lambda: [
            {
                "id": "abcd1234",
                "provider": "codex",
                "provider_session_id": None,
                "provider_log_path": None,
                "mapping_confidence": "none",
            }
        ],
    )

    gates = inbox_service.evaluate_jsonl_canary_gates()
    assert gates["overall_pass"] is False
    assert gates["gates"]["unmapped_active_terminals"]["pass"] is False
    assert gates["unmapped_terminal_ids"] == ["abcd1234"]


def test_canary_gate_treats_disagreement_as_advisory_by_default(monkeypatch):
    monkeypatch.setattr(inbox_service, "CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT", False)
    monkeypatch.setattr(
        inbox_service,
        "get_inbox_telemetry_snapshot",
        lambda: {
            "rates": {
                "jsonl_vs_tmux_disagreement_rate": 1.0,
                "fallback_trigger_rate": 0.0,
                "watcher_error_rate": 0.0,
            }
        },
    )
    monkeypatch.setattr(
        inbox_service.jsonl_status_engine,
        "get_telemetry_snapshot",
        lambda: {"lines_seen": 10, "malformed_lines": 0},
    )
    monkeypatch.setattr(inbox_service, "list_terminals", lambda: [])

    gates = inbox_service.evaluate_jsonl_canary_gates()
    assert gates["overall_pass"] is True
    assert gates["gates"]["jsonl_vs_tmux_disagreement_rate"]["enforced"] is False
    assert gates["gates"]["jsonl_vs_tmux_disagreement_rate"]["pass"] is True


def test_canary_gate_can_enforce_disagreement(monkeypatch):
    monkeypatch.setattr(inbox_service, "CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT", True)
    monkeypatch.setattr(
        inbox_service,
        "get_inbox_telemetry_snapshot",
        lambda: {
            "rates": {
                "jsonl_vs_tmux_disagreement_rate": 1.0,
                "fallback_trigger_rate": 0.0,
                "watcher_error_rate": 0.0,
            }
        },
    )
    monkeypatch.setattr(
        inbox_service.jsonl_status_engine,
        "get_telemetry_snapshot",
        lambda: {"lines_seen": 10, "malformed_lines": 0},
    )
    monkeypatch.setattr(inbox_service, "list_terminals", lambda: [])

    gates = inbox_service.evaluate_jsonl_canary_gates()
    assert gates["overall_pass"] is False
    assert gates["gates"]["jsonl_vs_tmux_disagreement_rate"]["enforced"] is True
    assert gates["gates"]["jsonl_vs_tmux_disagreement_rate"]["pass"] is False


def test_canary_gate_rollout_scope_ignores_non_rollout_unmapped(monkeypatch):
    monkeypatch.setattr(inbox_service, "CAO_JSONL_ROLLOUT_TERMINAL_IDS", set())
    monkeypatch.setattr(inbox_service, "CAO_JSONL_ROLLOUT_SESSION_NAMES", {"cao-rollout"})
    monkeypatch.setattr(
        inbox_service,
        "get_inbox_telemetry_snapshot",
        lambda: {
            "rates": {
                "rollout_jsonl_vs_tmux_disagreement_rate": 0.0,
                "rollout_fallback_trigger_rate": 0.0,
                "rollout_watcher_error_rate": 0.0,
            }
        },
    )
    monkeypatch.setattr(
        inbox_service.jsonl_status_engine,
        "get_telemetry_snapshot",
        lambda: {"lines_seen": 10, "malformed_lines": 0},
    )
    monkeypatch.setattr(
        inbox_service,
        "list_terminals",
        lambda: [
            {
                "id": "roll1",
                "provider": "claude_code",
                "tmux_session": "cao-rollout",
                "provider_session_id": "sid-1",
                "provider_log_path": "/tmp/sid-1.jsonl",
                "mapping_confidence": "high",
            },
            {
                "id": "nonroll-unmapped",
                "provider": "claude_code",
                "tmux_session": "cao-other",
                "provider_session_id": None,
                "provider_log_path": None,
                "mapping_confidence": "none",
            },
        ],
    )

    gates = inbox_service.evaluate_jsonl_canary_gates()
    assert gates["overall_pass"] is True
    assert gates["gates"]["unmapped_rollout_terminals"]["pass"] is True
    assert gates["gates"]["unmapped_active_terminals"]["pass"] is True
    assert gates["unmapped_terminal_ids"] == []


def test_sync_approval_state_creates_pending_request(monkeypatch):
    inbox_service.reset_inbox_telemetry()
    monkeypatch.setattr(inbox_service, "CAO_APPROVAL_QUEUE_ENABLED", True)
    monkeypatch.setattr(
        inbox_service,
        "create_or_get_pending_approval",
        lambda *_args, **_kwargs: (SimpleNamespace(id=7), True),
    )
    monkeypatch.setattr(
        inbox_service,
        "_extract_waiting_prompt_excerpt",
        lambda _tid: "Allow this action? (y/n)",
    )

    inbox_service.sync_approval_state_for_terminal(
        "abcd1234",
        TerminalStatus.WAITING_USER_ANSWER,
        metadata={"provider": "codex", "status_reason_code": "waiting"},
        source="poll",
    )

    telemetry = inbox_service.get_inbox_telemetry_snapshot()
    assert telemetry["approval_requests_created"] == 1


def test_sync_approval_state_resolves_when_not_waiting(monkeypatch):
    inbox_service.reset_inbox_telemetry()
    monkeypatch.setattr(inbox_service, "CAO_APPROVAL_QUEUE_ENABLED", True)
    monkeypatch.setattr(
        inbox_service,
        "resolve_pending_approvals_for_terminal",
        lambda *_args, **_kwargs: 1,
    )

    inbox_service.sync_approval_state_for_terminal(
        "abcd1234",
        TerminalStatus.IDLE,
        metadata={"provider": "codex", "status_reason_code": "ready"},
        source="poll",
    )

    telemetry = inbox_service.get_inbox_telemetry_snapshot()
    assert telemetry["approval_requests_resolved"] == 1
