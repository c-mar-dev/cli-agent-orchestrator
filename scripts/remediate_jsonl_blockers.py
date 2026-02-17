#!/usr/bin/env python3
"""Remediate JSONL migration blockers.

Actions:
1) Remove stale terminal rows whose tmux window no longer exists.
2) Remove orphaned pending inbox rows (missing receiver terminal or stale receiver).
3) Backfill launch_cwd for active terminals.
4) Attempt deterministic JSONL mapping capture for active Claude/Codex terminals.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from typing import Any, Dict, List

from cli_agent_orchestrator.clients.database import (
    InboxModel,
    SessionLocal,
    TerminalModel,
    init_db,
    list_terminals,
)
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.inbox import MessageStatus
from cli_agent_orchestrator.parsing.jsonl_status_engine import jsonl_status_engine

JSONL_PROVIDERS = {ProviderType.CLAUDE_CODE.value, ProviderType.CODEX.value}


@dataclass
class RemediationSummary:
    stale_terminals_detected: int = 0
    stale_terminals_deleted: int = 0
    orphan_pending_detected: int = 0
    orphan_pending_deleted: int = 0
    active_terminals: int = 0
    launch_cwd_backfilled: int = 0
    jsonl_mapping_attempted: int = 0
    jsonl_mapping_deterministic: int = 0
    jsonl_mapping_nondeterministic: int = 0
    active_jsonl_unmapped_after: int = 0


def _window_exists(session_name: str, window_name: str) -> bool:
    if not session_name or not window_name:
        return False
    if not tmux_client.session_exists(session_name):
        return False
    windows = tmux_client.get_session_windows(session_name)
    return any(w.get("name") == window_name for w in windows)


def _collect_terminals() -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    terminals = list_terminals()
    active: List[Dict[str, Any]] = []
    stale: List[Dict[str, Any]] = []
    for t in terminals:
        if _window_exists(str(t.get("tmux_session", "")), str(t.get("tmux_window", ""))):
            active.append(t)
        else:
            stale.append(t)
    return active, stale


def _delete_orphan_pending_and_stale_terminals(
    stale_ids: List[str], dry_run: bool
) -> tuple[int, int]:
    stale_set = set(stale_ids)
    all_terminal_ids = {str(t.get("id")) for t in list_terminals() if t.get("id")}

    with SessionLocal() as db:
        pending_rows = (
            db.query(InboxModel).filter(InboxModel.status == MessageStatus.PENDING.value).all()
        )
        orphan_pending_ids = [
            row.id
            for row in pending_rows
            if row.receiver_id not in all_terminal_ids or row.receiver_id in stale_set
        ]

        orphan_pending_count = len(orphan_pending_ids)
        stale_terminal_count = len(stale_ids)

        if not dry_run:
            if orphan_pending_ids:
                (
                    db.query(InboxModel)
                    .filter(InboxModel.id.in_(orphan_pending_ids))
                    .delete(synchronize_session=False)
                )
            if stale_ids:
                (
                    db.query(TerminalModel)
                    .filter(TerminalModel.id.in_(stale_ids))
                    .delete(synchronize_session=False)
                )
            db.commit()

    deleted_pending = 0 if dry_run else orphan_pending_count
    deleted_terminals = 0 if dry_run else stale_terminal_count
    return orphan_pending_count, deleted_pending, stale_terminal_count, deleted_terminals


def _backfill_and_map_active(
    active_terminals: List[Dict[str, Any]], dry_run: bool
) -> tuple[int, int, int, int]:
    launch_cwd_backfilled = 0
    mapping_attempted = 0
    mapping_deterministic = 0
    mapping_nondeterministic = 0

    for t in active_terminals:
        terminal_id = str(t["id"])
        provider = str(t.get("provider", ""))
        session_name = str(t.get("tmux_session", ""))
        window_name = str(t.get("tmux_window", ""))

        if not t.get("launch_cwd"):
            cwd = tmux_client.get_pane_working_directory(session_name, window_name)
            if cwd:
                launch_cwd_backfilled += 1
                if not dry_run:
                    with SessionLocal() as db:
                        row = (
                            db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
                        )
                        if row is not None:
                            row.launch_cwd = cwd
                            db.commit()

        if provider in JSONL_PROVIDERS:
            mapping_attempted += 1
            if dry_run:
                continue
            mapping = jsonl_status_engine.capture_startup_mapping(
                provider,
                terminal_id,
                session_name,
                window_name,
            )
            if mapping.deterministic:
                mapping_deterministic += 1
            else:
                mapping_nondeterministic += 1

    return (
        launch_cwd_backfilled,
        mapping_attempted,
        mapping_deterministic,
        mapping_nondeterministic,
    )


def _count_active_jsonl_unmapped() -> int:
    active, _stale = _collect_terminals()
    count = 0
    for t in active:
        if t.get("provider") not in JSONL_PROVIDERS:
            continue
        if (
            not t.get("launch_cwd")
            or not t.get("provider_session_id")
            or not t.get("provider_log_path")
            or t.get("mapping_confidence") in (None, "none")
        ):
            count += 1
    return count


def run(dry_run: bool) -> RemediationSummary:
    init_db()
    summary = RemediationSummary()

    active, stale = _collect_terminals()
    stale_ids = [str(t["id"]) for t in stale]

    summary.active_terminals = len(active)
    summary.stale_terminals_detected = len(stale_ids)

    (
        summary.orphan_pending_detected,
        summary.orphan_pending_deleted,
        summary.stale_terminals_detected,
        summary.stale_terminals_deleted,
    ) = _delete_orphan_pending_and_stale_terminals(stale_ids, dry_run=dry_run)

    # Recompute active list post-delete for deterministic operations.
    active_after, _stale_after = _collect_terminals()
    summary.active_terminals = len(active_after)

    (
        summary.launch_cwd_backfilled,
        summary.jsonl_mapping_attempted,
        summary.jsonl_mapping_deterministic,
        summary.jsonl_mapping_nondeterministic,
    ) = _backfill_and_map_active(active_after, dry_run=dry_run)

    summary.active_jsonl_unmapped_after = _count_active_jsonl_unmapped()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Remediate JSONL migration blockers")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report changes; do not modify DB/mappings",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = run(dry_run=args.dry_run)
    print(json.dumps(summary.__dict__, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
