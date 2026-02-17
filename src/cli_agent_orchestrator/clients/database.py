"""Database client for terminal metadata, inbox delivery, and approval queue state."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from cli_agent_orchestrator.constants import (
    CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT,
    CAO_INBOX_MAX_DELIVERY_ATTEMPTS,
    DATABASE_URL,
    DB_DIR,
)
from cli_agent_orchestrator.models.approval import ApprovalRequest, ApprovalStatus
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus

logger = logging.getLogger(__name__)

_INBOX_DB_TELEMETRY_LOCK = Lock()
_INBOX_DB_TELEMETRY: Dict[str, int] = {
    "idempotency_conflict_hits": 0,
    "duplicate_rows_pruned": 0,
    "dead_letter_requeue_attempted": 0,
    "dead_letter_requeue_applied": 0,
}

# Python 3.12 deprecates sqlite's built-in datetime adapter.
sqlite3.register_adapter(datetime, lambda value: value.isoformat(sep=" "))

Base: Any = declarative_base()


class TerminalModel(Base):
    """SQLAlchemy model for terminal metadata only."""

    __tablename__ = "terminals"

    id = Column(String, primary_key=True)  # "abc123ef"
    tmux_session = Column(String, nullable=False)  # "cao-session-name"
    tmux_window = Column(String, nullable=False)  # "window-name"
    provider = Column(String, nullable=False)  # "q_cli", "claude_code"
    agent_profile = Column(String)  # "developer", "reviewer" (optional)
    created_at = Column(DateTime, default=datetime.now)
    launch_cwd = Column(String, nullable=True)
    provider_session_id = Column(String, nullable=True)
    provider_log_path = Column(String, nullable=True)
    status_source = Column(String, nullable=False, default="tmux")
    mapping_confidence = Column(String, nullable=True)
    status_reason_code = Column(String, nullable=True)
    last_active = Column(DateTime, default=datetime.now)


class InboxModel(Base):
    """SQLAlchemy model for inbox messages."""

    __tablename__ = "inbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sender_id = Column(String, nullable=False)
    receiver_id = Column(String, nullable=False)
    message = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=False)
    status = Column(String, nullable=False)  # MessageStatus enum value
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=CAO_INBOX_MAX_DELIVERY_ATTEMPTS)
    next_attempt_at = Column(DateTime, nullable=False, default=datetime.now)
    last_attempt_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    failure_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now)


class ApprovalModel(Base):
    """SQLAlchemy model for explicit approval queue entries."""

    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    terminal_id = Column(String, nullable=False)
    provider = Column(String, nullable=True)
    status_reason_code = Column(String, nullable=True)
    prompt_excerpt = Column(String, nullable=True)
    source = Column(String, nullable=False, default="status")
    status = Column(String, nullable=False, default=ApprovalStatus.PENDING.value)
    acknowledged_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    resolution_sender_id = Column(String, nullable=True)
    resolution_message = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now)


class FlowModel(Base):
    """SQLAlchemy model for flow metadata."""

    __tablename__ = "flows"

    name = Column(String, primary_key=True)
    file_path = Column(String, nullable=False)
    schedule = Column(String, nullable=False)
    agent_profile = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    script = Column(String, nullable=True)
    last_run = Column(DateTime, nullable=True)
    next_run = Column(DateTime, nullable=True)
    enabled = Column(Boolean, default=True)


# Module-level singletons
DB_DIR.mkdir(parents=True, exist_ok=True)
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _bump_inbox_db_counter(counter: str, amount: int = 1) -> None:
    with _INBOX_DB_TELEMETRY_LOCK:
        _INBOX_DB_TELEMETRY[counter] = int(_INBOX_DB_TELEMETRY.get(counter, 0)) + int(amount)


def get_inbox_db_telemetry_snapshot() -> Dict[str, int]:
    """Return DB-level inbox telemetry counters."""
    with _INBOX_DB_TELEMETRY_LOCK:
        return dict(_INBOX_DB_TELEMETRY)


def reset_inbox_db_telemetry() -> None:
    """Reset DB-level inbox telemetry counters."""
    with _INBOX_DB_TELEMETRY_LOCK:
        for key in list(_INBOX_DB_TELEMETRY.keys()):
            _INBOX_DB_TELEMETRY[key] = 0


def init_db() -> None:
    """Initialize database tables and additive schema updates."""
    Base.metadata.create_all(bind=engine)
    _ensure_terminal_columns()
    _ensure_inbox_columns()
    _ensure_approval_columns()


def _ensure_terminal_columns() -> None:
    """Ensure terminal table has all runtime metadata columns."""
    required_columns = {
        "created_at": "DATETIME",
        "launch_cwd": "TEXT",
        "provider_session_id": "TEXT",
        "provider_log_path": "TEXT",
        "status_source": "TEXT DEFAULT 'tmux'",
        "mapping_confidence": "TEXT",
        "status_reason_code": "TEXT",
    }
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(terminals)")).fetchall()
        existing = {str(row[1]) for row in rows}
        for col_name, col_type in required_columns.items():
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE terminals ADD COLUMN {col_name} {col_type}"))
                logger.info("Added missing terminals column: %s", col_name)

        conn.execute(
            text(
                "UPDATE terminals SET created_at = COALESCE(created_at, last_active, CURRENT_TIMESTAMP)"
            )
        )


def _dedupe_inbox_idempotency_rows(conn: Any) -> Tuple[int, int]:
    """Prune duplicate inbox idempotency groups and keep oldest canonical row per key."""
    duplicate_groups = conn.execute(
        text(
            """
            SELECT receiver_id, idempotency_key, COUNT(*) AS row_count
            FROM inbox
            GROUP BY receiver_id, idempotency_key
            HAVING COUNT(*) > 1
            """
        )
    ).fetchall()

    if not duplicate_groups:
        return 0, 0

    now = datetime.now()
    pruned_rows = 0
    for receiver_id, idempotency_key, _row_count in duplicate_groups:
        rows = conn.execute(
            text(
                """
                SELECT id
                FROM inbox
                WHERE receiver_id = :receiver_id
                  AND idempotency_key = :idempotency_key
                ORDER BY (created_at IS NULL) ASC, created_at ASC, id ASC
                """
            ),
            {
                "receiver_id": receiver_id,
                "idempotency_key": idempotency_key,
            },
        ).fetchall()
        if len(rows) <= 1:
            continue

        for row in rows[1:]:
            duplicate_id = int(row[0])
            conn.execute(
                text(
                    """
                    UPDATE inbox
                    SET status = :status,
                        failed_at = :failed_at,
                        next_attempt_at = :next_attempt_at,
                        failure_reason = :failure_reason,
                        idempotency_key = :dedupe_key
                    WHERE id = :id
                    """
                ),
                {
                    "status": MessageStatus.DEAD_LETTER.value,
                    "failed_at": now,
                    "next_attempt_at": now,
                    "failure_reason": "duplicate-pruned",
                    "dedupe_key": f"{idempotency_key}::duplicate-pruned::{duplicate_id}",
                    "id": duplicate_id,
                },
            )
            pruned_rows += 1

    return len(duplicate_groups), pruned_rows


def _ensure_inbox_idempotency_index(conn: Any) -> None:
    """Ensure unique inbox idempotency index exists after deterministic dedupe."""
    duplicate_groups, pruned_rows = _dedupe_inbox_idempotency_rows(conn)
    if pruned_rows:
        _bump_inbox_db_counter("duplicate_rows_pruned", pruned_rows)
    logger.info(
        "Inbox idempotency dedupe summary: duplicate_groups=%s pruned_rows=%s",
        duplicate_groups,
        pruned_rows,
    )

    conn.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_receiver_idempotency_unique
            ON inbox(receiver_id, idempotency_key)
            """
        )
    )
    logger.info("Ensured inbox idempotency unique index exists")


def _ensure_inbox_columns() -> None:
    """Ensure inbox table supports retries/backoff/idempotency semantics."""
    required_columns = {
        "idempotency_key": "TEXT",
        "attempt_count": "INTEGER DEFAULT 0",
        "max_attempts": f"INTEGER DEFAULT {int(CAO_INBOX_MAX_DELIVERY_ATTEMPTS)}",
        "next_attempt_at": "DATETIME",
        "last_attempt_at": "DATETIME",
        "delivered_at": "DATETIME",
        "failed_at": "DATETIME",
        "failure_reason": "TEXT",
    }
    with engine.begin() as conn:
        rows = conn.execute(text("PRAGMA table_info(inbox)")).fetchall()
        existing = {str(row[1]) for row in rows}
        for col_name, col_type in required_columns.items():
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE inbox ADD COLUMN {col_name} {col_type}"))
                logger.info("Added missing inbox column: %s", col_name)

        conn.execute(
            text(
                "UPDATE inbox "
                "SET idempotency_key = COALESCE(idempotency_key, printf('legacy-%d', id)), "
                "attempt_count = COALESCE(attempt_count, 0), "
                f"max_attempts = COALESCE(max_attempts, {int(CAO_INBOX_MAX_DELIVERY_ATTEMPTS)}), "
                "next_attempt_at = COALESCE(next_attempt_at, created_at, CURRENT_TIMESTAMP)"
            )
        )
        _ensure_inbox_idempotency_index(conn)


def _ensure_approval_columns() -> None:
    """Ensure approvals table exists for explicit approval queue state."""
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='approvals'")
        ).fetchall()
        if rows:
            return
        ApprovalModel.__table__.create(bind=conn)
        logger.info("Created approvals table")


def _row_to_inbox_message(msg: InboxModel) -> InboxMessage:
    return InboxMessage(
        id=msg.id,
        sender_id=msg.sender_id,
        receiver_id=msg.receiver_id,
        message=msg.message,
        idempotency_key=msg.idempotency_key,
        status=MessageStatus(msg.status),
        attempt_count=msg.attempt_count,
        max_attempts=msg.max_attempts,
        next_attempt_at=msg.next_attempt_at,
        last_attempt_at=msg.last_attempt_at,
        delivered_at=msg.delivered_at,
        failed_at=msg.failed_at,
        failure_reason=msg.failure_reason,
        created_at=msg.created_at,
    )


def _row_to_approval_request(row: ApprovalModel) -> ApprovalRequest:
    return ApprovalRequest(
        id=row.id,
        terminal_id=row.terminal_id,
        provider=row.provider,
        status_reason_code=row.status_reason_code,
        prompt_excerpt=row.prompt_excerpt,
        source=row.source,
        status=ApprovalStatus(row.status),
        acknowledged_at=row.acknowledged_at,
        resolved_at=row.resolved_at,
        resolution_sender_id=row.resolution_sender_id,
        resolution_message=row.resolution_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _default_idempotency_key(sender_id: str, receiver_id: str, message: str) -> str:
    payload = f"{sender_id}\n{receiver_id}\n{message}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def create_terminal(
    terminal_id: str,
    tmux_session: str,
    tmux_window: str,
    provider: str,
    agent_profile: Optional[str] = None,
    launch_cwd: Optional[str] = None,
) -> Dict[str, Any]:
    """Create terminal metadata record."""
    with SessionLocal() as db:
        terminal = TerminalModel(
            id=terminal_id,
            tmux_session=tmux_session,
            tmux_window=tmux_window,
            provider=provider,
            agent_profile=agent_profile,
            launch_cwd=launch_cwd,
            status_source="tmux",
            mapping_confidence="none",
        )
        db.add(terminal)
        db.commit()
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "created_at": terminal.created_at,
            "launch_cwd": terminal.launch_cwd,
            "provider_session_id": terminal.provider_session_id,
            "provider_log_path": terminal.provider_log_path,
            "status_source": terminal.status_source,
            "mapping_confidence": terminal.mapping_confidence,
            "status_reason_code": terminal.status_reason_code,
        }


def get_terminal_metadata(terminal_id: str) -> Optional[Dict[str, Any]]:
    """Get terminal metadata by ID."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            logger.warning("Terminal metadata not found for terminal_id: %s", terminal_id)
            return None
        logger.debug(
            "Retrieved terminal metadata for %s: provider=%s, session=%s",
            terminal_id,
            terminal.provider,
            terminal.tmux_session,
        )
        return {
            "id": terminal.id,
            "tmux_session": terminal.tmux_session,
            "tmux_window": terminal.tmux_window,
            "provider": terminal.provider,
            "agent_profile": terminal.agent_profile,
            "created_at": terminal.created_at,
            "launch_cwd": terminal.launch_cwd,
            "provider_session_id": terminal.provider_session_id,
            "provider_log_path": terminal.provider_log_path,
            "status_source": terminal.status_source,
            "mapping_confidence": terminal.mapping_confidence,
            "status_reason_code": terminal.status_reason_code,
            "last_active": terminal.last_active,
        }


def list_terminals_by_session(tmux_session: str) -> List[Dict[str, Any]]:
    """List all terminals in a tmux session."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).all()
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "created_at": t.created_at,
                "launch_cwd": t.launch_cwd,
                "provider_session_id": t.provider_session_id,
                "provider_log_path": t.provider_log_path,
                "status_source": t.status_source,
                "mapping_confidence": t.mapping_confidence,
                "status_reason_code": t.status_reason_code,
                "last_active": t.last_active,
            }
            for t in terminals
        ]


def list_terminals() -> List[Dict[str, Any]]:
    """List all terminals."""
    with SessionLocal() as db:
        terminals = db.query(TerminalModel).order_by(TerminalModel.created_at.asc()).all()
        return [
            {
                "id": t.id,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider": t.provider,
                "agent_profile": t.agent_profile,
                "created_at": t.created_at,
                "launch_cwd": t.launch_cwd,
                "provider_session_id": t.provider_session_id,
                "provider_log_path": t.provider_log_path,
                "status_source": t.status_source,
                "mapping_confidence": t.mapping_confidence,
                "status_reason_code": t.status_reason_code,
                "last_active": t.last_active,
            }
            for t in terminals
        ]


def list_terminals_by_provider_log_path(provider_log_path: str) -> List[Dict[str, Any]]:
    """List terminals mapped to a specific provider JSONL log path."""
    with SessionLocal() as db:
        terminals = (
            db.query(TerminalModel)
            .filter(TerminalModel.provider_log_path == provider_log_path)
            .order_by(TerminalModel.created_at.asc())
            .all()
        )
        return [
            {
                "id": t.id,
                "provider": t.provider,
                "tmux_session": t.tmux_session,
                "tmux_window": t.tmux_window,
                "provider_session_id": t.provider_session_id,
                "provider_log_path": t.provider_log_path,
                "mapping_confidence": t.mapping_confidence,
                "status_source": t.status_source,
            }
            for t in terminals
        ]


def update_last_active(terminal_id: str) -> bool:
    """Update last active timestamp."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if terminal:
            terminal.last_active = datetime.now()
            db.commit()
            return True
        return False


def update_terminal_mapping(
    terminal_id: str,
    *,
    provider_session_id: Optional[str] = None,
    provider_log_path: Optional[str] = None,
    status_source: Optional[str] = None,
    mapping_confidence: Optional[str] = None,
    status_reason_code: Optional[str] = None,
) -> bool:
    """Update terminal JSONL/session mapping metadata."""
    with SessionLocal() as db:
        terminal = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).first()
        if not terminal:
            return False
        if provider_session_id is not None:
            terminal.provider_session_id = provider_session_id
        if provider_log_path is not None:
            terminal.provider_log_path = provider_log_path
        if status_source is not None:
            terminal.status_source = status_source
        if mapping_confidence is not None:
            terminal.mapping_confidence = mapping_confidence
        if status_reason_code is not None:
            terminal.status_reason_code = status_reason_code
        db.commit()
        return True


def delete_terminal(terminal_id: str) -> bool:
    """Delete terminal metadata."""
    with SessionLocal() as db:
        deleted = db.query(TerminalModel).filter(TerminalModel.id == terminal_id).delete()
        db.commit()
        return deleted > 0


def delete_terminals_by_session(tmux_session: str) -> int:
    """Delete all terminals in a session."""
    with SessionLocal() as db:
        deleted = (
            db.query(TerminalModel).filter(TerminalModel.tmux_session == tmux_session).delete()
        )
        db.commit()
        return deleted


def _get_canonical_inbox_row(db: Any, receiver_id: str, idempotency_key: str) -> Optional[InboxModel]:
    return (
        db.query(InboxModel)
        .filter(
            InboxModel.receiver_id == receiver_id,
            InboxModel.idempotency_key == idempotency_key,
        )
        .order_by(InboxModel.created_at.asc(), InboxModel.id.asc())
        .first()
    )


def create_inbox_message(
    sender_id: str,
    receiver_id: str,
    message: str,
    *,
    idempotency_key: Optional[str] = None,
    max_attempts: Optional[int] = None,
    requeue_terminal_state: bool = CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT,
) -> InboxMessage:
    """Create inbox message with retry metadata and idempotency semantics."""
    resolved_key = (idempotency_key or _default_idempotency_key(sender_id, receiver_id, message)).strip()
    if not resolved_key:
        resolved_key = _default_idempotency_key(sender_id, receiver_id, message)
    explicit_max_attempts = max_attempts is not None
    resolved_attempts = int(
        max_attempts if explicit_max_attempts else CAO_INBOX_MAX_DELIVERY_ATTEMPTS
    )
    if resolved_attempts < 1:
        resolved_attempts = 1

    created_now = datetime.now()
    with SessionLocal() as db:
        insert_result = db.execute(
            text(
                """
                INSERT OR IGNORE INTO inbox (
                    sender_id,
                    receiver_id,
                    message,
                    idempotency_key,
                    status,
                    attempt_count,
                    max_attempts,
                    next_attempt_at,
                    created_at
                )
                VALUES (
                    :sender_id,
                    :receiver_id,
                    :message,
                    :idempotency_key,
                    :status,
                    :attempt_count,
                    :max_attempts,
                    :next_attempt_at,
                    :created_at
                )
                """
            ),
            {
                "sender_id": sender_id,
                "receiver_id": receiver_id,
                "message": message,
                "idempotency_key": resolved_key,
                "status": MessageStatus.PENDING.value,
                "attempt_count": 0,
                "max_attempts": resolved_attempts,
                "next_attempt_at": created_now,
                "created_at": created_now,
            },
        )
        inserted = bool(insert_result.rowcount)
        if not inserted:
            _bump_inbox_db_counter("idempotency_conflict_hits")

        existing = _get_canonical_inbox_row(db, receiver_id, resolved_key)
        if existing is None:
            db.rollback()
            raise RuntimeError(
                "Failed to resolve inbox message after insert conflict handling "
                f"(receiver_id={receiver_id}, idempotency_key={resolved_key})"
            )

        if existing.status in (MessageStatus.DEAD_LETTER.value, MessageStatus.FAILED.value):
            if requeue_terminal_state:
                _bump_inbox_db_counter("dead_letter_requeue_attempted")
                now = datetime.now()
                existing.status = MessageStatus.PENDING.value
                existing.attempt_count = 0
                if explicit_max_attempts:
                    existing.max_attempts = resolved_attempts
                existing.next_attempt_at = now
                existing.last_attempt_at = None
                existing.failed_at = None
                existing.failure_reason = None
                existing.delivered_at = None
                _bump_inbox_db_counter("dead_letter_requeue_applied")
                logger.info(
                    "Requeued terminal-state inbox message id=%s receiver=%s key=%s",
                    existing.id,
                    receiver_id,
                    resolved_key,
                )
            else:
                logger.info(
                    "Terminal-state inbox message retained without requeue id=%s receiver=%s key=%s",
                    existing.id,
                    receiver_id,
                    resolved_key,
                )

        db.commit()
        db.refresh(existing)
        return _row_to_inbox_message(existing)


def get_pending_messages(receiver_id: str, limit: int = 1) -> List[InboxMessage]:
    """Get delivery-eligible messages ordered by created_at ASC (oldest first)."""
    with SessionLocal() as db:
        now = datetime.now()
        messages = (
            db.query(InboxModel)
            .filter(
                InboxModel.receiver_id == receiver_id,
                InboxModel.status.in_([
                    MessageStatus.PENDING.value,
                    MessageStatus.RETRYING.value,
                ]),
                InboxModel.next_attempt_at <= now,
            )
            .order_by(InboxModel.created_at.asc())
            .limit(limit)
            .all()
        )
        return [_row_to_inbox_message(msg) for msg in messages]


def list_pending_message_receivers(limit: int = 200) -> List[str]:
    """List terminal IDs with at least one delivery-eligible inbox message."""
    with SessionLocal() as db:
        now = datetime.now()
        rows = (
            db.query(InboxModel.receiver_id)
            .filter(
                InboxModel.status.in_([
                    MessageStatus.PENDING.value,
                    MessageStatus.RETRYING.value,
                ]),
                InboxModel.next_attempt_at <= now,
            )
            .distinct()
            .limit(limit)
            .all()
        )
        return [str(row[0]) for row in rows if row and row[0]]


def get_inbox_messages(
    receiver_id: str,
    limit: int = 10,
    status: Optional[MessageStatus] = None,
) -> List[InboxMessage]:
    """Get inbox messages with optional status filter ordered by created_at ASC."""
    with SessionLocal() as db:
        query = db.query(InboxModel).filter(InboxModel.receiver_id == receiver_id)

        if status is not None:
            query = query.filter(InboxModel.status == status.value)

        messages = query.order_by(InboxModel.created_at.asc()).limit(limit).all()
        return [_row_to_inbox_message(msg) for msg in messages]


def get_inbox_message(message_id: int) -> Optional[InboxMessage]:
    """Get inbox message by ID."""
    with SessionLocal() as db:
        row = db.query(InboxModel).filter(InboxModel.id == message_id).first()
        if row is None:
            return None
        return _row_to_inbox_message(row)


def update_message_status(message_id: int, status: MessageStatus) -> bool:
    """Compatibility helper for direct status updates."""
    with SessionLocal() as db:
        message = db.query(InboxModel).filter(InboxModel.id == message_id).first()
        if message is None:
            return False

        now = datetime.now()
        message.status = status.value
        if status == MessageStatus.DELIVERED:
            message.delivered_at = now
            message.failed_at = None
            message.failure_reason = None
            message.next_attempt_at = now
        elif status in (MessageStatus.FAILED, MessageStatus.DEAD_LETTER):
            message.failed_at = now
            message.next_attempt_at = now
        db.commit()
        return True


def mark_message_delivered(message_id: int) -> bool:
    """Mark a message as delivered and clear retry metadata."""
    with SessionLocal() as db:
        message = db.query(InboxModel).filter(InboxModel.id == message_id).first()
        if message is None:
            return False
        now = datetime.now()
        message.status = MessageStatus.DELIVERED.value
        message.delivered_at = now
        message.next_attempt_at = now
        message.failure_reason = None
        db.commit()
        return True


def mark_message_delivery_failure(
    message_id: int,
    failure_reason: str,
    *,
    backoff_base_seconds: int,
    backoff_multiplier: float,
    backoff_max_seconds: int,
) -> Optional[MessageStatus]:
    """Record a failed send attempt and schedule retry or dead-letter state."""
    with SessionLocal() as db:
        message = db.query(InboxModel).filter(InboxModel.id == message_id).first()
        if message is None:
            return None

        now = datetime.now()
        next_attempt_count = int(message.attempt_count or 0) + 1
        message.attempt_count = next_attempt_count
        message.last_attempt_at = now
        message.failure_reason = failure_reason

        max_attempts = int(message.max_attempts or 1)
        if next_attempt_count >= max_attempts:
            message.status = MessageStatus.DEAD_LETTER.value
            message.failed_at = now
            message.next_attempt_at = now
            db.commit()
            return MessageStatus.DEAD_LETTER

        # Exponential backoff, bounded by configured max.
        retry_delay = float(backoff_base_seconds) * (float(backoff_multiplier) ** (next_attempt_count - 1))
        retry_delay = min(retry_delay, float(backoff_max_seconds))
        message.status = MessageStatus.RETRYING.value
        message.next_attempt_at = now + timedelta(seconds=max(1.0, retry_delay))
        db.commit()
        return MessageStatus.RETRYING


def mark_message_dead_letter(message_id: int, failure_reason: str) -> bool:
    """Mark message as dead-letter without retry scheduling."""
    with SessionLocal() as db:
        message = db.query(InboxModel).filter(InboxModel.id == message_id).first()
        if message is None:
            return False
        now = datetime.now()
        message.status = MessageStatus.DEAD_LETTER.value
        message.failed_at = now
        message.failure_reason = failure_reason
        message.next_attempt_at = now
        db.commit()
        return True


def create_or_get_pending_approval(
    terminal_id: str,
    *,
    provider: Optional[str],
    status_reason_code: Optional[str],
    prompt_excerpt: Optional[str],
    source: str,
) -> Tuple[ApprovalRequest, bool]:
    """Create one pending approval per terminal or refresh existing pending entry."""
    with SessionLocal() as db:
        existing = (
            db.query(ApprovalModel)
            .filter(
                ApprovalModel.terminal_id == terminal_id,
                ApprovalModel.status == ApprovalStatus.PENDING.value,
            )
            .order_by(ApprovalModel.created_at.desc())
            .first()
        )
        now = datetime.now()

        if existing is not None:
            updated = False
            if provider and provider != existing.provider:
                existing.provider = provider
                updated = True
            if status_reason_code and status_reason_code != existing.status_reason_code:
                existing.status_reason_code = status_reason_code
                updated = True
            if prompt_excerpt and prompt_excerpt != existing.prompt_excerpt:
                existing.prompt_excerpt = prompt_excerpt
                updated = True
            if source and source != existing.source:
                existing.source = source
                updated = True
            if updated:
                existing.updated_at = now
                db.commit()
            return _row_to_approval_request(existing), False

        approval = ApprovalModel(
            terminal_id=terminal_id,
            provider=provider,
            status_reason_code=status_reason_code,
            prompt_excerpt=prompt_excerpt,
            source=source,
            status=ApprovalStatus.PENDING.value,
            created_at=now,
            updated_at=now,
        )
        db.add(approval)
        db.commit()
        db.refresh(approval)
        return _row_to_approval_request(approval), True


def list_approval_requests(
    terminal_id: str,
    *,
    limit: int = 20,
    status: Optional[ApprovalStatus] = None,
) -> List[ApprovalRequest]:
    """List approval requests for a terminal, newest first."""
    with SessionLocal() as db:
        query = db.query(ApprovalModel).filter(ApprovalModel.terminal_id == terminal_id)
        if status is not None:
            query = query.filter(ApprovalModel.status == status.value)
        rows = query.order_by(ApprovalModel.created_at.desc()).limit(limit).all()
        return [_row_to_approval_request(row) for row in rows]


def get_approval_request(terminal_id: str, approval_id: int) -> Optional[ApprovalRequest]:
    """Get a specific approval request by terminal and ID."""
    with SessionLocal() as db:
        row = (
            db.query(ApprovalModel)
            .filter(ApprovalModel.terminal_id == terminal_id, ApprovalModel.id == approval_id)
            .first()
        )
        if row is None:
            return None
        return _row_to_approval_request(row)


def acknowledge_approval_request(terminal_id: str, approval_id: int) -> Optional[ApprovalRequest]:
    """Mark pending approval as acknowledged."""
    with SessionLocal() as db:
        row = (
            db.query(ApprovalModel)
            .filter(ApprovalModel.terminal_id == terminal_id, ApprovalModel.id == approval_id)
            .first()
        )
        if row is None:
            return None

        if row.status == ApprovalStatus.PENDING.value:
            now = datetime.now()
            row.status = ApprovalStatus.ACKNOWLEDGED.value
            row.acknowledged_at = now
            row.updated_at = now
            db.commit()
        return _row_to_approval_request(row)


def resolve_approval_request(
    terminal_id: str,
    approval_id: int,
    *,
    sender_id: Optional[str],
    resolution_message: Optional[str],
) -> Optional[ApprovalRequest]:
    """Mark approval request resolved."""
    with SessionLocal() as db:
        row = (
            db.query(ApprovalModel)
            .filter(ApprovalModel.terminal_id == terminal_id, ApprovalModel.id == approval_id)
            .first()
        )
        if row is None:
            return None

        now = datetime.now()
        row.status = ApprovalStatus.RESOLVED.value
        if row.acknowledged_at is None:
            row.acknowledged_at = now
        row.resolved_at = now
        row.resolution_sender_id = sender_id
        row.resolution_message = resolution_message
        row.updated_at = now
        db.commit()
        return _row_to_approval_request(row)


def resolve_pending_approvals_for_terminal(
    terminal_id: str,
    *,
    resolution_message: str,
) -> int:
    """Resolve all pending approvals for a terminal when it leaves WAITING state."""
    with SessionLocal() as db:
        rows = (
            db.query(ApprovalModel)
            .filter(
                ApprovalModel.terminal_id == terminal_id,
                ApprovalModel.status == ApprovalStatus.PENDING.value,
            )
            .all()
        )
        if not rows:
            return 0
        now = datetime.now()
        for row in rows:
            row.status = ApprovalStatus.RESOLVED.value
            row.acknowledged_at = row.acknowledged_at or now
            row.resolved_at = now
            row.resolution_message = resolution_message
            row.updated_at = now
        db.commit()
        return len(rows)


def count_pending_approvals_without_terminal() -> int:
    """Return count of pending approvals whose terminal metadata no longer exists."""
    with SessionLocal() as db:
        return int(
            db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM approvals a
                    LEFT JOIN terminals t ON t.id = a.terminal_id
                    WHERE a.status = :status
                      AND t.id IS NULL
                    """
                ),
                {"status": ApprovalStatus.PENDING.value},
            ).scalar()
            or 0
        )


# Flow database functions


def create_flow(
    name: str,
    file_path: str,
    schedule: str,
    agent_profile: str,
    provider: str,
    script: str,
    next_run: datetime,
) -> Flow:
    """Create flow record."""
    with SessionLocal() as db:
        flow = FlowModel(
            name=name,
            file_path=file_path,
            schedule=schedule,
            agent_profile=agent_profile,
            provider=provider,
            script=script,
            next_run=next_run,
        )
        db.add(flow)
        db.commit()
        db.refresh(flow)
        return Flow(
            name=flow.name,
            file_path=flow.file_path,
            schedule=flow.schedule,
            agent_profile=flow.agent_profile,
            provider=flow.provider,
            script=flow.script,
            last_run=flow.last_run,
            next_run=flow.next_run,
            enabled=flow.enabled,
        )


def get_flow(name: str) -> Optional[Flow]:
    """Get flow by name."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if not flow:
            return None
        return Flow(
            name=flow.name,
            file_path=flow.file_path,
            schedule=flow.schedule,
            agent_profile=flow.agent_profile,
            provider=flow.provider,
            script=flow.script,
            last_run=flow.last_run,
            next_run=flow.next_run,
            enabled=flow.enabled,
        )


def list_flows() -> List[Flow]:
    """List all flows."""
    with SessionLocal() as db:
        flows = db.query(FlowModel).order_by(FlowModel.next_run).all()
        return [
            Flow(
                name=f.name,
                file_path=f.file_path,
                schedule=f.schedule,
                agent_profile=f.agent_profile,
                provider=f.provider,
                script=f.script,
                last_run=f.last_run,
                next_run=f.next_run,
                enabled=f.enabled,
            )
            for f in flows
        ]


def update_flow_run_times(name: str, last_run: datetime, next_run: datetime) -> bool:
    """Update flow run times after execution."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if flow:
            flow.last_run = last_run
            flow.next_run = next_run
            db.commit()
            return True
        return False


def update_flow_enabled(name: str, enabled: bool, next_run: Optional[datetime] = None) -> bool:
    """Update flow enabled status and optionally next_run."""
    with SessionLocal() as db:
        flow = db.query(FlowModel).filter(FlowModel.name == name).first()
        if flow:
            flow.enabled = enabled
            if next_run is not None:
                flow.next_run = next_run
            db.commit()
            return True
        return False


def delete_flow(name: str) -> bool:
    """Delete flow."""
    with SessionLocal() as db:
        deleted = db.query(FlowModel).filter(FlowModel.name == name).delete()
        db.commit()
        return deleted > 0


def get_flows_to_run() -> List[Flow]:
    """Get enabled flows where next_run <= now."""
    with SessionLocal() as db:
        now = datetime.now()
        flows = (
            db.query(FlowModel).filter(FlowModel.enabled == True, FlowModel.next_run <= now).all()
        )
        return [
            Flow(
                name=f.name,
                file_path=f.file_path,
                schedule=f.schedule,
                agent_profile=f.agent_profile,
                provider=f.provider,
                script=f.script,
                last_run=f.last_run,
                next_run=f.next_run,
                enabled=f.enabled,
            )
            for f in flows
        ]
