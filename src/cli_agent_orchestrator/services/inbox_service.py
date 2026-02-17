"""Inbox service with watchdog for automatic message delivery."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileSystemEventHandler

from cli_agent_orchestrator.clients.database import (
    create_or_get_pending_approval,
    get_pending_messages,
    get_terminal_metadata,
    list_pending_message_receivers,
    list_terminals,
    list_terminals_by_provider_log_path,
    mark_message_dead_letter,
    mark_message_delivered,
    mark_message_delivery_failure,
    resolve_pending_approvals_for_terminal,
)
from cli_agent_orchestrator.constants import (
    CAO_APPROVAL_PROMPT_TAIL_LINES,
    CAO_APPROVAL_QUEUE_ENABLED,
    CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS,
    CAO_INBOX_RETRY_BACKOFF_MAX_SECONDS,
    CAO_INBOX_RETRY_BACKOFF_MULTIPLIER,
    CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT,
    CAO_JSONL_GATE_DISAGREEMENT_THRESHOLD,
    CAO_JSONL_GATE_FALLBACK_THRESHOLD,
    CAO_JSONL_TMUX_COMPARISON_ENABLED,
    CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD,
    CAO_JSONL_ROLLOUT_SESSION_NAMES,
    CAO_JSONL_ROLLOUT_TERMINAL_IDS,
    INBOX_SERVICE_TAIL_LINES,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.parsing.jsonl_status_engine import jsonl_status_engine
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import terminal_service

logger = logging.getLogger(__name__)

# Treat explicit "waiting for user" as deliverable so inbox messages can answer prompts.
READY_STATUSES = {
    TerminalStatus.IDLE,
    TerminalStatus.COMPLETED,
    TerminalStatus.WAITING_USER_ANSWER,
}
JSONL_PROVIDER_TYPES = {ProviderType.CLAUDE_CODE.value, ProviderType.CODEX.value}
WAITING_PROMPT_EXCERPT_PATTERN = re.compile(
    r"(Approve|Allow).*(y/n|yes/no|yes|no)", re.IGNORECASE
)


@dataclass
class _InboxTelemetry:
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trigger_log_events: int = 0
    trigger_jsonl_events: int = 0
    trigger_poll_events: int = 0
    rollout_trigger_log_events: int = 0
    rollout_trigger_jsonl_events: int = 0
    rollout_trigger_poll_events: int = 0
    jsonl_watch_enabled: bool = False
    jsonl_watcher_active: bool = False
    jsonl_watch_paths: List[str] = field(default_factory=list)
    jsonl_watcher_started_at: Optional[str] = None
    jsonl_watcher_events_received: int = 0
    last_jsonl_event_at: Optional[str] = None
    last_jsonl_event_path: Optional[str] = None
    status_checks_total: int = 0
    jsonl_status_checks: int = 0
    jsonl_fallback_checks: int = 0
    rollout_jsonl_status_checks: int = 0
    rollout_jsonl_fallback_checks: int = 0
    delivery_attempts: int = 0
    deliveries_succeeded: int = 0
    deliveries_failed: int = 0
    delivery_retries: int = 0
    delivery_dead_letters: int = 0
    rollout_delivery_attempts: int = 0
    rollout_deliveries_succeeded: int = 0
    rollout_deliveries_failed: int = 0
    watcher_errors: int = 0
    rollout_watcher_errors: int = 0
    jsonl_unmapped_events: int = 0
    jsonl_tmux_comparisons: int = 0
    jsonl_tmux_disagreements: int = 0
    rollout_jsonl_tmux_comparisons: int = 0
    rollout_jsonl_tmux_disagreements: int = 0
    approval_requests_created: int = 0
    approval_requests_resolved: int = 0
    last_disagreements: List[Dict[str, Any]] = field(default_factory=list)


_TELEMETRY = _InboxTelemetry()
_TELEMETRY_LOCK = threading.Lock()


def _statuses_equivalent_for_delivery(
    effective_status: TerminalStatus, tmux_status: TerminalStatus
) -> bool:
    if effective_status == tmux_status:
        return True
    return effective_status in READY_STATUSES and tmux_status in READY_STATUSES


def _rollout_scope_enabled() -> bool:
    return bool(CAO_JSONL_ROLLOUT_TERMINAL_IDS or CAO_JSONL_ROLLOUT_SESSION_NAMES)


def _is_rollout_terminal(
    terminal_id: str, metadata: Optional[Dict[str, Any]] = None
) -> bool:
    if not _rollout_scope_enabled():
        return True
    if terminal_id in CAO_JSONL_ROLLOUT_TERMINAL_IDS:
        return True
    meta = metadata or get_terminal_metadata(terminal_id) or {}
    session_name = str(meta.get("tmux_session") or "")
    return session_name in CAO_JSONL_ROLLOUT_SESSION_NAMES


def _telemetry_event(trigger_source: str, rollout_scope_terminal: bool) -> None:
    with _TELEMETRY_LOCK:
        if trigger_source == "jsonl":
            _TELEMETRY.trigger_jsonl_events += 1
            if rollout_scope_terminal:
                _TELEMETRY.rollout_trigger_jsonl_events += 1
        elif trigger_source == "poll":
            _TELEMETRY.trigger_poll_events += 1
            if rollout_scope_terminal:
                _TELEMETRY.rollout_trigger_poll_events += 1
        else:
            _TELEMETRY.trigger_log_events += 1
            if rollout_scope_terminal:
                _TELEMETRY.rollout_trigger_log_events += 1


def _telemetry_status_check(
    status_source: Optional[str], provider_uses_jsonl: bool, rollout_scope_terminal: bool
) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY.status_checks_total += 1
        if provider_uses_jsonl:
            _TELEMETRY.jsonl_status_checks += 1
            if rollout_scope_terminal:
                _TELEMETRY.rollout_jsonl_status_checks += 1
            if status_source != "jsonl":
                _TELEMETRY.jsonl_fallback_checks += 1
                if rollout_scope_terminal:
                    _TELEMETRY.rollout_jsonl_fallback_checks += 1


def _telemetry_record_delivery(success: bool, rollout_scope_terminal: bool) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY.delivery_attempts += 1
        if rollout_scope_terminal:
            _TELEMETRY.rollout_delivery_attempts += 1
        if success:
            _TELEMETRY.deliveries_succeeded += 1
            if rollout_scope_terminal:
                _TELEMETRY.rollout_deliveries_succeeded += 1
        else:
            _TELEMETRY.deliveries_failed += 1
            if rollout_scope_terminal:
                _TELEMETRY.rollout_deliveries_failed += 1


def _telemetry_record_retry(dead_letter: bool) -> None:
    with _TELEMETRY_LOCK:
        if dead_letter:
            _TELEMETRY.delivery_dead_letters += 1
        else:
            _TELEMETRY.delivery_retries += 1


def _telemetry_record_approval(created: bool = False, resolved: bool = False) -> None:
    with _TELEMETRY_LOCK:
        if created:
            _TELEMETRY.approval_requests_created += 1
        if resolved:
            _TELEMETRY.approval_requests_resolved += 1


def _telemetry_record_watcher_error(rollout_scope_terminal: bool = False) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY.watcher_errors += 1
        if rollout_scope_terminal:
            _TELEMETRY.rollout_watcher_errors += 1


def _telemetry_record_unmapped_jsonl() -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY.jsonl_unmapped_events += 1


def _telemetry_record_jsonl_watcher_event(log_path: Path) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY.jsonl_watcher_events_received += 1
        _TELEMETRY.last_jsonl_event_at = datetime.now(timezone.utc).isoformat()
        _TELEMETRY.last_jsonl_event_path = str(log_path)


def _telemetry_record_comparison(
    terminal_id: str,
    jsonl_or_effective_status: TerminalStatus,
    tmux_status: TerminalStatus,
    rollout_scope_terminal: bool,
) -> None:
    with _TELEMETRY_LOCK:
        _TELEMETRY.jsonl_tmux_comparisons += 1
        if rollout_scope_terminal:
            _TELEMETRY.rollout_jsonl_tmux_comparisons += 1
        if not _statuses_equivalent_for_delivery(jsonl_or_effective_status, tmux_status):
            _TELEMETRY.jsonl_tmux_disagreements += 1
            if rollout_scope_terminal:
                _TELEMETRY.rollout_jsonl_tmux_disagreements += 1
            _TELEMETRY.last_disagreements.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "terminal_id": terminal_id,
                    "effective_status": jsonl_or_effective_status.value,
                    "tmux_status": tmux_status.value,
                }
            )
            if len(_TELEMETRY.last_disagreements) > 25:
                _TELEMETRY.last_disagreements = _TELEMETRY.last_disagreements[-25:]


def reset_inbox_telemetry() -> None:
    """Reset inbox telemetry counters (used by tests/canary restarts)."""
    global _TELEMETRY
    with _TELEMETRY_LOCK:
        _TELEMETRY = _InboxTelemetry()


def reset_jsonl_canary_state(reset_parser: bool = True) -> Dict[str, Any]:
    """Reset inbox telemetry and optionally parser telemetry for a fresh canary window."""
    prior = get_inbox_telemetry_snapshot()
    reset_inbox_telemetry()
    configure_watcher_telemetry(
        jsonl_watch_enabled=bool(prior.get("jsonl_watch_enabled", False)),
        jsonl_watch_paths=list(prior.get("jsonl_watch_paths", [])),
        observer_started=bool(prior.get("jsonl_watcher_active", False)),
    )
    if reset_parser:
        jsonl_status_engine.reset_telemetry()
    return {
        "inbox_telemetry_reset": True,
        "parser_telemetry_reset": bool(reset_parser),
        "reset_at": datetime.now(timezone.utc).isoformat(),
    }


def configure_watcher_telemetry(
    *, jsonl_watch_enabled: bool, jsonl_watch_paths: List[str], observer_started: bool
) -> None:
    """Record watcher runtime configuration and startup state."""
    with _TELEMETRY_LOCK:
        _TELEMETRY.jsonl_watch_enabled = jsonl_watch_enabled
        _TELEMETRY.jsonl_watcher_active = observer_started and jsonl_watch_enabled
        _TELEMETRY.jsonl_watch_paths = list(jsonl_watch_paths)
        _TELEMETRY.jsonl_watcher_started_at = (
            datetime.now(timezone.utc).isoformat()
            if _TELEMETRY.jsonl_watcher_active
            else _TELEMETRY.jsonl_watcher_started_at
        )


def get_inbox_telemetry_snapshot() -> Dict[str, Any]:
    """Return telemetry snapshot for diagnostics and canary gate checks."""
    with _TELEMETRY_LOCK:
        comparisons = _TELEMETRY.jsonl_tmux_comparisons
        jsonl_checks = _TELEMETRY.jsonl_status_checks
        rollout_comparisons = _TELEMETRY.rollout_jsonl_tmux_comparisons
        rollout_jsonl_checks = _TELEMETRY.rollout_jsonl_status_checks
        events_total = (
            _TELEMETRY.trigger_log_events
            + _TELEMETRY.trigger_jsonl_events
            + _TELEMETRY.trigger_poll_events
        )
        rollout_events_total = (
            _TELEMETRY.rollout_trigger_log_events
            + _TELEMETRY.rollout_trigger_jsonl_events
            + _TELEMETRY.rollout_trigger_poll_events
        )
        return {
            "started_at": _TELEMETRY.started_at.isoformat(),
            "trigger_log_events": _TELEMETRY.trigger_log_events,
            "trigger_jsonl_events": _TELEMETRY.trigger_jsonl_events,
            "trigger_poll_events": _TELEMETRY.trigger_poll_events,
            "rollout_trigger_log_events": _TELEMETRY.rollout_trigger_log_events,
            "rollout_trigger_jsonl_events": _TELEMETRY.rollout_trigger_jsonl_events,
            "rollout_trigger_poll_events": _TELEMETRY.rollout_trigger_poll_events,
            "rollout_scope_enabled": _rollout_scope_enabled(),
            "rollout_scope_terminal_ids": sorted(CAO_JSONL_ROLLOUT_TERMINAL_IDS),
            "rollout_scope_session_names": sorted(CAO_JSONL_ROLLOUT_SESSION_NAMES),
            "jsonl_watch_enabled": _TELEMETRY.jsonl_watch_enabled,
            "jsonl_watcher_active": _TELEMETRY.jsonl_watcher_active,
            "jsonl_watch_paths": list(_TELEMETRY.jsonl_watch_paths),
            "jsonl_watcher_started_at": _TELEMETRY.jsonl_watcher_started_at,
            "jsonl_tmux_comparison_enabled": CAO_JSONL_TMUX_COMPARISON_ENABLED,
            "jsonl_watcher_events_received": _TELEMETRY.jsonl_watcher_events_received,
            "last_jsonl_event_at": _TELEMETRY.last_jsonl_event_at,
            "last_jsonl_event_path": _TELEMETRY.last_jsonl_event_path,
            "status_checks_total": _TELEMETRY.status_checks_total,
            "jsonl_status_checks": _TELEMETRY.jsonl_status_checks,
            "jsonl_fallback_checks": _TELEMETRY.jsonl_fallback_checks,
            "rollout_jsonl_status_checks": _TELEMETRY.rollout_jsonl_status_checks,
            "rollout_jsonl_fallback_checks": _TELEMETRY.rollout_jsonl_fallback_checks,
            "delivery_attempts": _TELEMETRY.delivery_attempts,
            "deliveries_succeeded": _TELEMETRY.deliveries_succeeded,
            "deliveries_failed": _TELEMETRY.deliveries_failed,
            "delivery_retries": _TELEMETRY.delivery_retries,
            "delivery_dead_letters": _TELEMETRY.delivery_dead_letters,
            "rollout_delivery_attempts": _TELEMETRY.rollout_delivery_attempts,
            "rollout_deliveries_succeeded": _TELEMETRY.rollout_deliveries_succeeded,
            "rollout_deliveries_failed": _TELEMETRY.rollout_deliveries_failed,
            "watcher_errors": _TELEMETRY.watcher_errors,
            "rollout_watcher_errors": _TELEMETRY.rollout_watcher_errors,
            "jsonl_unmapped_events": _TELEMETRY.jsonl_unmapped_events,
            "jsonl_tmux_comparisons": comparisons,
            "jsonl_tmux_disagreements": _TELEMETRY.jsonl_tmux_disagreements,
            "rollout_jsonl_tmux_comparisons": rollout_comparisons,
            "rollout_jsonl_tmux_disagreements": _TELEMETRY.rollout_jsonl_tmux_disagreements,
            "approval_requests_created": _TELEMETRY.approval_requests_created,
            "approval_requests_resolved": _TELEMETRY.approval_requests_resolved,
            "events_total": events_total,
            "rollout_events_total": rollout_events_total,
            "rates": {
                "jsonl_vs_tmux_disagreement_rate": (
                    _TELEMETRY.jsonl_tmux_disagreements / comparisons if comparisons else 0.0
                ),
                "fallback_trigger_rate": (
                    _TELEMETRY.jsonl_fallback_checks / jsonl_checks if jsonl_checks else 0.0
                ),
                "watcher_error_rate": (
                    _TELEMETRY.watcher_errors / events_total if events_total else 0.0
                ),
                "rollout_jsonl_vs_tmux_disagreement_rate": (
                    _TELEMETRY.rollout_jsonl_tmux_disagreements / rollout_comparisons
                    if rollout_comparisons
                    else 0.0
                ),
                "rollout_fallback_trigger_rate": (
                    _TELEMETRY.rollout_jsonl_fallback_checks / rollout_jsonl_checks
                    if rollout_jsonl_checks
                    else 0.0
                ),
                "rollout_watcher_error_rate": (
                    _TELEMETRY.rollout_watcher_errors / rollout_events_total
                    if rollout_events_total
                    else 0.0
                ),
                "dead_letter_rate": (
                    _TELEMETRY.delivery_dead_letters / _TELEMETRY.delivery_attempts
                    if _TELEMETRY.delivery_attempts
                    else 0.0
                ),
            },
            "last_disagreements": list(_TELEMETRY.last_disagreements),
        }


def evaluate_jsonl_canary_gates() -> Dict[str, Any]:
    """Evaluate JSONL migration gates against current telemetry."""
    inbox_metrics = get_inbox_telemetry_snapshot()
    parser_metrics = jsonl_status_engine.get_telemetry_snapshot()

    managed_terminals = [t for t in list_terminals() if t.get("provider") in JSONL_PROVIDER_TYPES]
    rollout_terminals = [t for t in managed_terminals if _is_rollout_terminal(str(t["id"]), t)]
    unmapped_active_terminals = [
        t["id"]
        for t in rollout_terminals
        if not t.get("provider_session_id")
        or not t.get("provider_log_path")
        or t.get("mapping_confidence") in (None, "none")
    ]

    parser_lines_seen = int(parser_metrics.get("lines_seen", 0))
    parser_malformed = int(parser_metrics.get("malformed_lines", 0))
    parse_failure_rate = (parser_malformed / parser_lines_seen) if parser_lines_seen else 0.0

    rates = inbox_metrics.get("rates", {})
    disagreement_rate = float(
        rates.get(
            "rollout_jsonl_vs_tmux_disagreement_rate",
            rates.get("jsonl_vs_tmux_disagreement_rate", 0.0),
        )
    )
    fallback_rate = float(
        rates.get("rollout_fallback_trigger_rate", rates.get("fallback_trigger_rate", 0.0))
    )
    watcher_error_rate = float(
        rates.get("rollout_watcher_error_rate", rates.get("watcher_error_rate", 0.0))
    )
    disagreement_enforced = bool(CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT)
    comparison_enabled = bool(inbox_metrics.get("jsonl_tmux_comparison_enabled", False))
    if disagreement_enforced:
        disagreement_pass = (
            comparison_enabled and disagreement_rate < CAO_JSONL_GATE_DISAGREEMENT_THRESHOLD
        )
    else:
        disagreement_pass = True

    gates = {
        "jsonl_vs_tmux_disagreement_rate": {
            "threshold": CAO_JSONL_GATE_DISAGREEMENT_THRESHOLD,
            "value": disagreement_rate,
            "pass": disagreement_pass,
            "enforced": disagreement_enforced,
            "comparison_enabled": comparison_enabled,
        },
        "fallback_trigger_rate": {
            "threshold": CAO_JSONL_GATE_FALLBACK_THRESHOLD,
            "value": fallback_rate,
            "pass": fallback_rate < CAO_JSONL_GATE_FALLBACK_THRESHOLD,
        },
        "unmapped_rollout_terminals": {
            "threshold": 0,
            "value": len(unmapped_active_terminals),
            "pass": len(unmapped_active_terminals) == 0,
        },
        # Backward-compatible alias for existing tooling.
        "unmapped_active_terminals": {
            "threshold": 0,
            "value": len(unmapped_active_terminals),
            "pass": len(unmapped_active_terminals) == 0,
        },
        "watcher_error_rate": {
            "threshold": CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD,
            "value": watcher_error_rate,
            "pass": watcher_error_rate < CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD,
        },
        "parser_malformed_line_rate": {
            "threshold": CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD,
            "value": parse_failure_rate,
            "pass": parse_failure_rate < CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD,
        },
    }

    startup_attempts = int(parser_metrics.get("startup_mapping_attempts", 0))
    startup_deterministic = int(parser_metrics.get("startup_mapping_deterministic", 0))
    startup_success_rate = (
        startup_deterministic / startup_attempts if startup_attempts else 1.0
    )
    ambiguous_count = int(
        (parser_metrics.get("mapping_reason_counts", {}) or {}).get(
            "claude_ambiguous_parent_sessions", 0
        )
    )
    mapping_demotions = int(parser_metrics.get("mapping_demotions", 0))
    hook_escalation = {
        "triggered": any(
            [
                mapping_demotions > 0,
                fallback_rate >= 0.05,
                startup_success_rate < 0.99,
                ambiguous_count >= 2,
            ]
        ),
        "conditions": {
            "mapping_demotions_gt_zero": mapping_demotions > 0,
            "fallback_rate_gte_5pct": fallback_rate >= 0.05,
            "startup_mapping_success_lt_99pct": startup_success_rate < 0.99,
            "repeated_claude_ambiguous_parent_sessions": ambiguous_count >= 2,
        },
        "metrics": {
            "mapping_demotions": mapping_demotions,
            "fallback_rate": fallback_rate,
            "startup_mapping_success_rate": startup_success_rate,
            "claude_ambiguous_parent_sessions": ambiguous_count,
        },
    }

    overall_pass = all(gate["pass"] for gate in gates.values())
    return {
        "overall_pass": overall_pass,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "rollout_scope": {
            "enabled": _rollout_scope_enabled(),
            "terminal_ids": sorted(CAO_JSONL_ROLLOUT_TERMINAL_IDS),
            "session_names": sorted(CAO_JSONL_ROLLOUT_SESSION_NAMES),
            "managed_terminal_count": len(managed_terminals),
            "rollout_terminal_count": len(rollout_terminals),
        },
        "gates": gates,
        "unmapped_terminal_ids": unmapped_active_terminals,
        "hook_escalation": hook_escalation,
        "inbox_metrics": inbox_metrics,
        "parser_metrics": parser_metrics,
    }


def _get_log_tail(terminal_id: str, lines: int = 5) -> str:
    """Get last N lines from terminal log file."""
    log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), str(log_path)], capture_output=True, text=True, timeout=1
        )
        return result.stdout
    except Exception:
        return ""


def _extract_waiting_prompt_excerpt(terminal_id: str) -> Optional[str]:
    """Extract a concise prompt snippet for approval queue visibility."""
    tail = _get_log_tail(terminal_id, lines=CAO_APPROVAL_PROMPT_TAIL_LINES)
    if not tail:
        return None
    lines = [line.strip() for line in tail.splitlines() if line.strip()]
    for line in reversed(lines):
        if WAITING_PROMPT_EXCERPT_PATTERN.search(line):
            return line[:400]
    if not lines:
        return None
    return lines[-1][:400]


def sync_approval_state_for_terminal(
    terminal_id: str,
    status: TerminalStatus,
    *,
    metadata: Optional[Dict[str, Any]] = None,
    source: str = "status",
) -> None:
    """Keep explicit approval queue aligned with terminal waiting state."""
    if not CAO_APPROVAL_QUEUE_ENABLED:
        return

    try:
        if status == TerminalStatus.WAITING_USER_ANSWER:
            refreshed = metadata or get_terminal_metadata(terminal_id) or {}
            approval, created = create_or_get_pending_approval(
                terminal_id,
                provider=refreshed.get("provider"),
                status_reason_code=refreshed.get("status_reason_code"),
                prompt_excerpt=_extract_waiting_prompt_excerpt(terminal_id),
                source=source,
            )
            if created:
                logger.info(
                    "Created approval request id=%s terminal=%s source=%s",
                    approval.id,
                    terminal_id,
                    source,
                )
                _telemetry_record_approval(created=True)
            return

        resolved = resolve_pending_approvals_for_terminal(
            terminal_id,
            resolution_message="terminal-no-longer-waiting",
        )
        if resolved:
            logger.info("Resolved %s pending approval(s) terminal=%s", resolved, terminal_id)
            _telemetry_record_approval(resolved=True)
    except Exception as exc:
        logger.debug("Approval queue sync skipped terminal=%s: %s", terminal_id, exc)


def _has_idle_pattern(terminal_id: str) -> bool:
    """Check if log tail contains idle pattern without expensive tmux calls."""
    try:
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return False
        # JSONL-backed providers do structured status checks, so bypass regex fast-check.
        if provider.uses_jsonl_status():
            return True
    except Exception:
        return False

    tail = _get_log_tail(terminal_id)
    if not tail:
        return False

    try:
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return False
        idle_pattern = provider.get_idle_pattern_for_log()
        return bool(re.search(idle_pattern, tail))
    except Exception:
        return False


def _resolve_terminals_for_jsonl_path(log_path: Path) -> List[str]:
    """Resolve terminal IDs associated with a JSONL file path."""
    direct = list_terminals_by_provider_log_path(str(log_path))
    terminal_ids = [str(t["id"]) for t in direct if t.get("id")]
    if terminal_ids:
        return terminal_ids

    # Attempt to prime mappings only for terminals that currently have pending work.
    pending_receivers = list_pending_message_receivers(limit=200)
    matched: List[str] = []
    for terminal_id in pending_receivers:
        metadata = get_terminal_metadata(terminal_id) or {}
        provider_type = metadata.get("provider")
        if provider_type not in JSONL_PROVIDER_TYPES:
            continue

        try:
            provider = provider_manager.get_provider(terminal_id)
            if provider is None or not provider.uses_jsonl_status():
                continue
            # Force one status read to refresh mapping metadata.
            provider.get_status(tail_lines=INBOX_SERVICE_TAIL_LINES)
            refreshed = get_terminal_metadata(terminal_id) or {}
            if refreshed.get("provider_log_path") == str(log_path):
                matched.append(terminal_id)
        except Exception as exc:
            logger.debug("Failed to prime JSONL mapping for terminal=%s: %s", terminal_id, exc)

    return matched


def _fail_orphan_pending_messages(terminal_id: str, batch_size: int = 100) -> int:
    """Mark pending messages failed when their receiver terminal no longer exists."""
    failed = 0
    while True:
        pending = get_pending_messages(terminal_id, limit=batch_size)
        if not pending:
            break
        for msg in pending:
            mark_message_dead_letter(msg.id, "receiver-terminal-missing")
            failed += 1
        if len(pending) < batch_size:
            break
    return failed


def check_and_send_pending_messages(terminal_id: str, trigger_source: str = "manual") -> bool:
    """Check for pending messages and send if terminal is ready.

    Args:
        terminal_id: Terminal ID to check messages for
        trigger_source: Signal source (`manual`, `terminal_log`, `jsonl`, `poll`)

    Returns:
        bool: True if a message was sent, False otherwise
    """
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        failed = _fail_orphan_pending_messages(terminal_id)
        if failed:
            logger.warning(
                "Marked %s pending inbox message(s) dead-letter for unknown terminal %s",
                failed,
                terminal_id,
            )
        return False
    metadata = metadata or {}
    rollout_scope_terminal = _is_rollout_terminal(terminal_id, metadata)
    _telemetry_event(trigger_source, rollout_scope_terminal=rollout_scope_terminal)

    # Get provider and check status (also keeps explicit approval queue in sync).
    provider = provider_manager.get_provider(terminal_id)
    if provider is None:
        raise ValueError(f"Provider not found for terminal {terminal_id}")

    status = provider.get_status(tail_lines=INBOX_SERVICE_TAIL_LINES)
    refreshed_metadata = get_terminal_metadata(terminal_id) or metadata
    status_source = refreshed_metadata.get("status_source")
    sync_approval_state_for_terminal(
        terminal_id,
        status,
        metadata=refreshed_metadata,
        source=trigger_source,
    )

    _telemetry_status_check(
        status_source=status_source,
        provider_uses_jsonl=provider.uses_jsonl_status(),
        rollout_scope_terminal=rollout_scope_terminal,
    )

    if CAO_JSONL_TMUX_COMPARISON_ENABLED:
        tmux_status = provider.get_tmux_status(tail_lines=INBOX_SERVICE_TAIL_LINES)
        if tmux_status is not None:
            _telemetry_record_comparison(
                terminal_id=terminal_id,
                jsonl_or_effective_status=status,
                tmux_status=tmux_status,
                rollout_scope_terminal=rollout_scope_terminal,
            )

    if status not in READY_STATUSES:
        logger.debug(f"Terminal {terminal_id} not ready (status={status})")
        return False

    # Check for pending messages only after readiness/approval state update.
    messages = get_pending_messages(terminal_id, limit=1)
    if not messages:
        return False
    message = messages[0]

    # Send message
    try:
        terminal_service.send_input(terminal_id, message.message)
        mark_message_delivered(message.id)
        _telemetry_record_delivery(success=True, rollout_scope_terminal=rollout_scope_terminal)
        logger.info(f"Delivered message {message.id} to terminal {terminal_id}")
        return True
    except Exception as exc:
        logger.error(f"Failed to send message {message.id} to {terminal_id}: {exc}")
        _telemetry_record_delivery(success=False, rollout_scope_terminal=rollout_scope_terminal)
        retry_status = mark_message_delivery_failure(
            message.id,
            str(exc),
            backoff_base_seconds=CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS,
            backoff_multiplier=CAO_INBOX_RETRY_BACKOFF_MULTIPLIER,
            backoff_max_seconds=CAO_INBOX_RETRY_BACKOFF_MAX_SECONDS,
        )
        _telemetry_record_retry(dead_letter=retry_status == MessageStatus.DEAD_LETTER)
        return False


def poll_pending_deliveries_once() -> int:
    """Safety-net poll that checks pending inbox receivers once.

    Returns the number of deliveries performed.
    """
    delivered = 0
    for receiver_id in list_pending_message_receivers(limit=500):
        try:
            if check_and_send_pending_messages(receiver_id, trigger_source="poll"):
                delivered += 1
        except Exception as exc:
            _telemetry_record_watcher_error(
                rollout_scope_terminal=_is_rollout_terminal(receiver_id)
            )
            logger.error("Error polling inbox delivery for %s: %s", receiver_id, exc)
    return delivered


class LogFileHandler(FileSystemEventHandler):
    """Handler for terminal log file changes."""

    def on_modified(self, event):
        """Handle file modification events."""
        if isinstance(event, FileModifiedEvent) and event.src_path.endswith(".log"):
            log_path = Path(event.src_path)
            terminal_id = log_path.stem
            logger.debug(f"Log file modified: {terminal_id}.log")
            self._handle_log_change(terminal_id)

    def on_created(self, event):
        """Handle file creation events."""
        if isinstance(event, FileCreatedEvent) and event.src_path.endswith(".log"):
            log_path = Path(event.src_path)
            terminal_id = log_path.stem
            logger.debug(f"Log file created: {terminal_id}.log")
            self._handle_log_change(terminal_id)

    def _handle_log_change(self, terminal_id: str):
        """Handle log file change and attempt message delivery."""
        try:
            # Check for pending messages first
            messages = get_pending_messages(terminal_id, limit=1)
            if not messages:
                logger.debug(f"No pending messages for {terminal_id}, skipping")
                return

            # Fast check: does log tail have idle pattern?
            if not _has_idle_pattern(terminal_id):
                logger.debug(
                    f"Terminal {terminal_id} not idle (no idle pattern in log tail), skipping"
                )
                return

            # Attempt delivery
            check_and_send_pending_messages(terminal_id, trigger_source="terminal_log")

        except Exception as exc:
            _telemetry_record_watcher_error(rollout_scope_terminal=_is_rollout_terminal(terminal_id))
            logger.error(f"Error handling log change for {terminal_id}: {exc}")


class JsonlFileHandler(FileSystemEventHandler):
    """Handler for provider JSONL log changes."""

    def on_modified(self, event):
        if isinstance(event, FileModifiedEvent) and event.src_path.endswith(".jsonl"):
            self._handle_jsonl_change(Path(event.src_path))

    def on_created(self, event):
        if isinstance(event, FileCreatedEvent) and event.src_path.endswith(".jsonl"):
            self._handle_jsonl_change(Path(event.src_path))

    def _handle_jsonl_change(self, log_path: Path) -> None:
        _telemetry_record_jsonl_watcher_event(log_path)
        terminal_ids = _resolve_terminals_for_jsonl_path(log_path)
        if not terminal_ids:
            _telemetry_record_unmapped_jsonl()
            return

        for terminal_id in terminal_ids:
            try:
                check_and_send_pending_messages(terminal_id, trigger_source="jsonl")
            except Exception as exc:
                _telemetry_record_watcher_error(
                    rollout_scope_terminal=_is_rollout_terminal(terminal_id)
                )
                logger.error(
                    "Error handling JSONL change for terminal=%s path=%s: %s",
                    terminal_id,
                    log_path,
                    exc,
                )
