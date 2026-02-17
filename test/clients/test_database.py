"""Direct tests for database client functions using in-memory SQLite."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.approval import ApprovalStatus
from cli_agent_orchestrator.models.inbox import MessageStatus


class TestTerminalDatabase:
    def test_create_and_get_terminal(self, in_memory_db):
        created = database.create_terminal(
            terminal_id="abc123ef",
            tmux_session="cao-s1",
            tmux_window="dev-1",
            provider="q_cli",
            agent_profile="developer",
            launch_cwd="/tmp/project",
        )

        assert created["id"] == "abc123ef"
        metadata = database.get_terminal_metadata("abc123ef")
        assert metadata is not None
        assert metadata["tmux_session"] == "cao-s1"
        assert metadata["tmux_window"] == "dev-1"
        assert metadata["provider"] == "q_cli"
        assert metadata["agent_profile"] == "developer"
        assert metadata["launch_cwd"] == "/tmp/project"

    def test_create_terminal_duplicate_id_raises(self, in_memory_db):
        database.create_terminal("deadbeef", "cao-s1", "w1", "q_cli")

        with pytest.raises(IntegrityError):
            database.create_terminal("deadbeef", "cao-s2", "w2", "codex")

    def test_list_terminals_empty(self, in_memory_db):
        assert database.list_terminals() == []

    def test_list_terminals_ordered_by_created_at(self, in_memory_db):
        database.create_terminal("aaaaaaaa", "cao-s1", "w1", "q_cli")
        database.create_terminal("bbbbbbbb", "cao-s1", "w2", "q_cli")

        with database.SessionLocal() as db:
            t1 = db.query(database.TerminalModel).filter_by(id="aaaaaaaa").first()
            t2 = db.query(database.TerminalModel).filter_by(id="bbbbbbbb").first()
            assert t1 is not None and t2 is not None
            t1.created_at = datetime(2025, 1, 1, 0, 0, 1)
            t2.created_at = datetime(2025, 1, 1, 0, 0, 2)
            db.commit()

        terminals = database.list_terminals()
        assert [t["id"] for t in terminals] == ["aaaaaaaa", "bbbbbbbb"]

    def test_list_terminals_by_session(self, in_memory_db):
        database.create_terminal("aaaaaaaa", "cao-s1", "w1", "q_cli")
        database.create_terminal("bbbbbbbb", "cao-s2", "w2", "q_cli")

        s1 = database.list_terminals_by_session("cao-s1")
        assert [t["id"] for t in s1] == ["aaaaaaaa"]

    def test_delete_terminal_returns_true(self, in_memory_db):
        database.create_terminal("aaaaaaaa", "cao-s1", "w1", "q_cli")

        assert database.delete_terminal("aaaaaaaa") is True
        assert database.get_terminal_metadata("aaaaaaaa") is None

    def test_delete_terminal_returns_false_when_missing(self, in_memory_db):
        assert database.delete_terminal("ffffffff") is False

    def test_delete_terminals_by_session_returns_count(self, in_memory_db):
        database.create_terminal("aaaaaaaa", "cao-s1", "w1", "q_cli")
        database.create_terminal("bbbbbbbb", "cao-s1", "w2", "q_cli")
        database.create_terminal("cccccccc", "cao-s2", "w3", "q_cli")

        deleted = database.delete_terminals_by_session("cao-s1")
        assert deleted == 2
        assert len(database.list_terminals()) == 1

    def test_update_last_active_changes_timestamp(self, in_memory_db):
        database.create_terminal("aaaaaaaa", "cao-s1", "w1", "q_cli")

        before = database.get_terminal_metadata("aaaaaaaa")
        assert before is not None
        old_last_active = before["last_active"]

        assert database.update_last_active("aaaaaaaa") is True
        after = database.get_terminal_metadata("aaaaaaaa")
        assert after is not None
        assert after["last_active"] >= old_last_active

    def test_update_last_active_returns_false_when_missing(self, in_memory_db):
        assert database.update_last_active("ffffffff") is False

    @pytest.mark.parametrize(
        ("kwargs", "field", "expected"),
        [
            ({"provider_session_id": "sess-1"}, "provider_session_id", "sess-1"),
            ({"provider_log_path": "/tmp/p.jsonl"}, "provider_log_path", "/tmp/p.jsonl"),
            ({"status_source": "jsonl"}, "status_source", "jsonl"),
            ({"mapping_confidence": "high"}, "mapping_confidence", "high"),
            ({"status_reason_code": "match"}, "status_reason_code", "match"),
        ],
    )
    def test_update_terminal_mapping_each_field(self, in_memory_db, kwargs, field, expected):
        database.create_terminal("aaaaaaaa", "cao-s1", "w1", "q_cli")

        assert database.update_terminal_mapping("aaaaaaaa", **kwargs) is True
        metadata = database.get_terminal_metadata("aaaaaaaa")
        assert metadata is not None
        assert metadata[field] == expected

    def test_update_terminal_mapping_nonexistent_returns_false(self, in_memory_db):
        assert database.update_terminal_mapping("ffffffff", provider_session_id="s1") is False

    def test_ensure_terminal_columns_backfills_existing_db(self, in_memory_db, monkeypatch):
        # Recreate legacy terminals table missing modern columns.
        with database.engine.begin() as conn:
            conn.execute(text("DROP TABLE terminals"))
            conn.execute(
                text(
                    """
                    CREATE TABLE terminals (
                        id TEXT PRIMARY KEY,
                        tmux_session TEXT NOT NULL,
                        tmux_window TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        agent_profile TEXT,
                        last_active DATETIME
                    )
                    """
                )
            )
            conn.execute(
                text(
                    """
                    INSERT INTO terminals (id, tmux_session, tmux_window, provider, agent_profile, last_active)
                    VALUES (:id, :tmux_session, :tmux_window, :provider, :agent_profile, :last_active)
                    """
                ),
                {
                    "id": "legacy001",
                    "tmux_session": "cao-legacy",
                    "tmux_window": "win-1",
                    "provider": "q_cli",
                    "agent_profile": "developer",
                    "last_active": datetime(2024, 1, 1, 12, 0, 0),
                },
            )

        database._ensure_terminal_columns()

        with database.engine.begin() as conn:
            columns = {
                row[1] for row in conn.execute(text("PRAGMA table_info(terminals)")).fetchall()
            }
            for required in {
                "created_at",
                "launch_cwd",
                "provider_session_id",
                "provider_log_path",
                "status_source",
                "mapping_confidence",
                "status_reason_code",
            }:
                assert required in columns

            row = conn.execute(
                text("SELECT created_at, last_active FROM terminals WHERE id='legacy001'")
            ).fetchone()
            assert row is not None
            assert row[0] is not None


class TestInboxDatabase:
    def test_create_inbox_message_defaults_to_pending(self, in_memory_db):
        message = database.create_inbox_message("sender", "recv", "hello")
        assert message.status == MessageStatus.PENDING
        assert message.receiver_id == "recv"

    def test_get_pending_messages_fifo(self, in_memory_db):
        first = database.create_inbox_message("s1", "recv", "one")
        second = database.create_inbox_message("s2", "recv", "two")

        pending = database.get_pending_messages("recv", limit=10)
        assert [m.id for m in pending] == [first.id, second.id]

    def test_get_pending_messages_limit(self, in_memory_db):
        database.create_inbox_message("s1", "recv", "one")
        database.create_inbox_message("s2", "recv", "two")

        pending = database.get_pending_messages("recv", limit=1)
        assert len(pending) == 1

    def test_get_pending_messages_ignores_delivered(self, in_memory_db):
        msg = database.create_inbox_message("s1", "recv", "one")
        database.update_message_status(msg.id, MessageStatus.DELIVERED)

        assert database.get_pending_messages("recv", limit=10) == []

    def test_list_pending_message_receivers_returns_distinct_ids(self, in_memory_db):
        database.create_inbox_message("s1", "recv-a", "one")
        database.create_inbox_message("s2", "recv-a", "two")
        database.create_inbox_message("s3", "recv-b", "three")

        receivers = sorted(database.list_pending_message_receivers())
        assert receivers == ["recv-a", "recv-b"]

    def test_get_inbox_messages_with_status_filter(self, in_memory_db):
        pending = database.create_inbox_message("s1", "recv", "one")
        delivered = database.create_inbox_message("s2", "recv", "two")
        database.update_message_status(delivered.id, MessageStatus.DELIVERED)

        delivered_only = database.get_inbox_messages("recv", status=MessageStatus.DELIVERED)
        assert [m.id for m in delivered_only] == [delivered.id]

        pending_only = database.get_inbox_messages("recv", status=MessageStatus.PENDING)
        assert [m.id for m in pending_only] == [pending.id]

    def test_update_message_status_transitions(self, in_memory_db):
        msg = database.create_inbox_message("s1", "recv", "one")

        assert database.update_message_status(msg.id, MessageStatus.DELIVERED) is True
        delivered = database.get_inbox_messages("recv", status=MessageStatus.DELIVERED)
        assert [m.id for m in delivered] == [msg.id]

    def test_update_message_status_nonexistent_returns_false(self, in_memory_db):
        assert database.update_message_status(9999, MessageStatus.DELIVERED) is False

    def test_create_inbox_message_idempotency_returns_existing(self, in_memory_db):
        first = database.create_inbox_message(
            "s1",
            "recv",
            "same payload",
            idempotency_key="fixed-key",
        )
        second = database.create_inbox_message(
            "s1",
            "recv",
            "same payload",
            idempotency_key="fixed-key",
        )
        assert first.id == second.id

    def test_create_inbox_message_idempotency_concurrent_returns_single_canonical_row(
        self,
        tmp_path,
        monkeypatch,
    ):
        race_engine = create_engine(
            f"sqlite:///{tmp_path / 'race-idempotency.db'}",
            connect_args={"check_same_thread": False},
        )
        race_session = sessionmaker(autocommit=False, autoflush=False, bind=race_engine)
        monkeypatch.setattr(database, "engine", race_engine)
        monkeypatch.setattr(database, "SessionLocal", race_session)
        database.Base.metadata.drop_all(bind=race_engine)
        database.Base.metadata.create_all(bind=race_engine)
        database.init_db()

        def _create() -> int:
            msg = database.create_inbox_message(
                "sender",
                "recv",
                "payload",
                idempotency_key="race-key",
            )
            return msg.id

        with ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(lambda _x: _create(), range(20)))

        assert len(set(ids)) == 1
        rows = database.get_inbox_messages("recv", status=MessageStatus.PENDING)
        assert len([row for row in rows if row.idempotency_key == "race-key"]) == 1
        race_engine.dispose()

    def test_create_inbox_message_requeue_terminal_state_true_resets_dead_letter_metadata(
        self,
        in_memory_db,
    ):
        msg = database.create_inbox_message(
            "s1",
            "recv",
            "retry me",
            idempotency_key="requeue-key",
            max_attempts=1,
        )
        state = database.mark_message_delivery_failure(
            msg.id,
            "first-failure",
            backoff_base_seconds=1,
            backoff_multiplier=2.0,
            backoff_max_seconds=30,
        )
        assert state == MessageStatus.DEAD_LETTER

        requeued = database.create_inbox_message(
            "s1",
            "recv",
            "retry me",
            idempotency_key="requeue-key",
            requeue_terminal_state=True,
            max_attempts=4,
        )
        assert requeued.id == msg.id
        assert requeued.status == MessageStatus.PENDING
        assert requeued.attempt_count == 0
        assert requeued.max_attempts == 4
        assert requeued.failure_reason is None
        assert requeued.failed_at is None

    def test_create_inbox_message_requeue_preserves_max_attempts_without_override(self, in_memory_db):
        msg = database.create_inbox_message(
            "s1",
            "recv",
            "retry me",
            idempotency_key="preserve-max-attempts",
            max_attempts=7,
        )
        for attempt in range(1, 8):
            state = database.mark_message_delivery_failure(
                msg.id,
                f"failure-{attempt}",
                backoff_base_seconds=1,
                backoff_multiplier=2.0,
                backoff_max_seconds=30,
            )
            expected = MessageStatus.DEAD_LETTER if attempt == 7 else MessageStatus.RETRYING
            assert state == expected

        requeued = database.create_inbox_message(
            "s1",
            "recv",
            "retry me",
            idempotency_key="preserve-max-attempts",
            requeue_terminal_state=True,
        )
        assert requeued.max_attempts == 7

    def test_create_inbox_message_requeue_terminal_state_false_preserves_dead_letter(self, in_memory_db):
        msg = database.create_inbox_message(
            "s1",
            "recv",
            "retry me",
            idempotency_key="no-requeue-key",
            max_attempts=1,
        )
        state = database.mark_message_delivery_failure(
            msg.id,
            "first-failure",
            backoff_base_seconds=1,
            backoff_multiplier=2.0,
            backoff_max_seconds=30,
        )
        assert state == MessageStatus.DEAD_LETTER

        retained = database.create_inbox_message(
            "s1",
            "recv",
            "retry me",
            idempotency_key="no-requeue-key",
            requeue_terminal_state=False,
        )
        assert retained.id == msg.id
        assert retained.status == MessageStatus.DEAD_LETTER
        assert retained.failure_reason == "first-failure"

    def test_create_inbox_message_requeue_does_not_mutate_delivered(self, in_memory_db):
        msg = database.create_inbox_message(
            "s1",
            "recv",
            "delivered",
            idempotency_key="delivered-key",
            max_attempts=2,
        )
        assert database.mark_message_delivered(msg.id) is True
        delivered_before = database.get_inbox_message(msg.id)
        assert delivered_before is not None
        delivered_at_before = delivered_before.delivered_at

        retained = database.create_inbox_message(
            "s1",
            "recv",
            "delivered",
            idempotency_key="delivered-key",
            requeue_terminal_state=True,
        )
        assert retained.id == msg.id
        assert retained.status == MessageStatus.DELIVERED
        assert retained.delivered_at == delivered_at_before

    def test_ensure_inbox_columns_dedupes_and_enforces_unique_index(self, in_memory_db):
        with database.engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS idx_inbox_receiver_idempotency_unique"))
            conn.execute(
                text(
                    """
                    INSERT INTO inbox (
                        sender_id, receiver_id, message, idempotency_key, status,
                        attempt_count, max_attempts, next_attempt_at, created_at
                    )
                    VALUES
                        ('s1', 'recv', 'one', 'legacy-key', 'pending', 0, 3, :t1, :t1),
                        ('s2', 'recv', 'two', 'legacy-key', 'pending', 0, 3, :t2, :t2)
                    """
                ),
                {
                    "t1": datetime(2026, 1, 1, 0, 0, 1),
                    "t2": datetime(2026, 1, 1, 0, 0, 2),
                },
            )

        database._ensure_inbox_columns()

        with database.engine.begin() as conn:
            canonical = conn.execute(
                text(
                    """
                    SELECT id, status, failure_reason
                    FROM inbox
                    WHERE receiver_id='recv' AND idempotency_key='legacy-key'
                    ORDER BY id ASC
                    """
                )
            ).fetchall()
            assert len(canonical) == 1
            assert canonical[0][1] == MessageStatus.PENDING.value

            pruned = conn.execute(
                text(
                    """
                    SELECT id, status, failure_reason, idempotency_key
                    FROM inbox
                    WHERE receiver_id='recv' AND failure_reason='duplicate-pruned'
                    ORDER BY id ASC
                    """
                )
            ).fetchall()
            assert len(pruned) == 1
            assert pruned[0][1] == MessageStatus.DEAD_LETTER.value
            assert "::duplicate-pruned::" in str(pruned[0][3])

            indexes = conn.execute(text("PRAGMA index_list(inbox)")).fetchall()
            matching = [idx for idx in indexes if idx[1] == "idx_inbox_receiver_idempotency_unique"]
            assert matching
            assert matching[0][2] == 1

    def test_mark_message_delivery_failure_retry_then_dead_letter(self, in_memory_db):
        msg = database.create_inbox_message(
            "s1",
            "recv",
            "retry me",
            max_attempts=2,
        )

        first = database.mark_message_delivery_failure(
            msg.id,
            "timeout",
            backoff_base_seconds=1,
            backoff_multiplier=2.0,
            backoff_max_seconds=30,
        )
        assert first == MessageStatus.RETRYING

        retrying = database.get_inbox_messages("recv", status=MessageStatus.RETRYING)
        assert len(retrying) == 1
        assert retrying[0].attempt_count == 1

        second = database.mark_message_delivery_failure(
            msg.id,
            "timeout-again",
            backoff_base_seconds=1,
            backoff_multiplier=2.0,
            backoff_max_seconds=30,
        )
        assert second == MessageStatus.DEAD_LETTER

        dead = database.get_inbox_messages("recv", status=MessageStatus.DEAD_LETTER)
        assert len(dead) == 1
        assert dead[0].attempt_count == 2
        assert dead[0].failure_reason == "timeout-again"

    def test_approval_request_lifecycle(self, in_memory_db):
        created, is_new = database.create_or_get_pending_approval(
            "term-1",
            provider="codex",
            status_reason_code="waiting",
            prompt_excerpt="Allow operation? (y/n)",
            source="status",
        )
        assert is_new is True
        assert created.status == ApprovalStatus.PENDING

        same, is_new_again = database.create_or_get_pending_approval(
            "term-1",
            provider="codex",
            status_reason_code="waiting",
            prompt_excerpt="Allow operation? (y/n)",
            source="status",
        )
        assert is_new_again is False
        assert same.id == created.id

        acked = database.acknowledge_approval_request("term-1", created.id)
        assert acked is not None
        assert acked.status == ApprovalStatus.ACKNOWLEDGED

        resolved = database.resolve_approval_request(
            "term-1",
            created.id,
            sender_id="human-1",
            resolution_message="yes",
        )
        assert resolved is not None
        assert resolved.status == ApprovalStatus.RESOLVED
        assert resolved.resolution_sender_id == "human-1"

        listed = database.list_approval_requests("term-1", status=ApprovalStatus.RESOLVED)
        assert len(listed) == 1
        assert listed[0].id == created.id

    def test_count_pending_approvals_without_terminal(self, in_memory_db):
        database.create_terminal("live0001", "cao-s1", "w1", "q_cli")
        database.create_or_get_pending_approval(
            "live0001",
            provider="q_cli",
            status_reason_code="waiting",
            prompt_excerpt="approve",
            source="status",
        )
        database.create_or_get_pending_approval(
            "ghost001",
            provider="q_cli",
            status_reason_code="waiting",
            prompt_excerpt="approve",
            source="status",
        )

        assert database.count_pending_approvals_without_terminal() == 1


class TestFlowDatabase:
    def test_create_and_get_flow(self, in_memory_db):
        next_run = datetime.now() + timedelta(minutes=5)
        created = database.create_flow(
            name="daily-report",
            file_path="/tmp/flow.md",
            schedule="*/5 * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=next_run,
        )

        fetched = database.get_flow("daily-report")
        assert fetched is not None
        assert fetched.name == created.name
        assert fetched.next_run == created.next_run

    def test_create_flow_duplicate_name_raises(self, in_memory_db):
        next_run = datetime.now() + timedelta(minutes=1)
        database.create_flow(
            name="dupe",
            file_path="/tmp/one.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=next_run,
        )

        with pytest.raises(IntegrityError):
            database.create_flow(
                name="dupe",
                file_path="/tmp/two.md",
                schedule="* * * * *",
                agent_profile="developer",
                provider="q_cli",
                script="",
                next_run=next_run,
            )

    def test_list_flows_ordered_by_next_run(self, in_memory_db):
        database.create_flow(
            name="late",
            file_path="/tmp/late.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=datetime(2026, 1, 1, 0, 0, 10),
        )
        database.create_flow(
            name="early",
            file_path="/tmp/early.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=datetime(2026, 1, 1, 0, 0, 1),
        )

        assert [f.name for f in database.list_flows()] == ["early", "late"]

    def test_update_flow_run_times(self, in_memory_db):
        database.create_flow(
            name="f1",
            file_path="/tmp/f1.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=datetime.now() + timedelta(minutes=1),
        )

        last_run = datetime.now()
        next_run = last_run + timedelta(hours=1)
        assert database.update_flow_run_times("f1", last_run=last_run, next_run=next_run) is True

        flow = database.get_flow("f1")
        assert flow is not None
        assert flow.last_run == last_run
        assert flow.next_run == next_run

    def test_update_flow_enabled_toggle(self, in_memory_db):
        database.create_flow(
            name="f1",
            file_path="/tmp/f1.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=datetime.now() + timedelta(minutes=1),
        )

        assert database.update_flow_enabled("f1", enabled=False) is True
        assert database.get_flow("f1") is not None
        assert database.get_flow("f1").enabled is False

    def test_delete_flow(self, in_memory_db):
        database.create_flow(
            name="f1",
            file_path="/tmp/f1.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=datetime.now() + timedelta(minutes=1),
        )

        assert database.delete_flow("f1") is True
        assert database.get_flow("f1") is None

    def test_get_flows_to_run_filters_enabled_and_past_due(self, in_memory_db):
        now = datetime.now()
        database.create_flow(
            name="past-enabled",
            file_path="/tmp/a.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=now - timedelta(minutes=1),
        )
        database.create_flow(
            name="future-enabled",
            file_path="/tmp/b.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=now + timedelta(minutes=10),
        )
        database.create_flow(
            name="past-disabled",
            file_path="/tmp/c.md",
            schedule="* * * * *",
            agent_profile="developer",
            provider="q_cli",
            script="",
            next_run=now - timedelta(minutes=5),
        )
        database.update_flow_enabled("past-disabled", enabled=False)

        to_run = database.get_flows_to_run()
        assert [f.name for f in to_run] == ["past-enabled"]
