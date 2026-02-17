# Plan: Fix Review Findings (Profile + Inbox + Approval Reliability)

## Context
This plan addresses all open findings from implementation review:
1. Inbox idempotency race under concurrency.
2. Dead-letter idempotency resend behavior undefined/blocked.
3. Pending approvals can remain stale when terminals are deleted.
4. Flow provider values are not validated at creation time.
5. `docs/api.md` does not match actual API/provider surface.
6. Runtime profile coverage is incomplete in tests.

## Goals
- Make inbox idempotency concurrency-safe and deterministic.
- Define and implement resend semantics for terminal inbox states.
- Ensure approval queue state is cleaned up during terminal lifecycle teardown.
- Fail fast on invalid flow provider values.
- Bring API documentation in sync with implementation.
- Expand test coverage for profile parsing/precedence and new lifecycle behavior.

## Non-Goals
- Changing core orchestration semantics outside inbox/approval/flow provider validation.
- Broad refactors of JSONL status engine.
- Introducing alembic migrations (continue additive migration helpers pattern).

## Workstreams

### WS1: Concurrency-safe inbox idempotency
Scope:
- `src/cli_agent_orchestrator/clients/database.py`
- DB schema migration path for existing local SQLite DBs

Changes:
- Add a unique constraint/index on `(receiver_id, idempotency_key)` for `inbox`.
- Harden migration path with explicit policy:
  - Detect duplicate rows before creating the unique index.
  - Canonical row policy: keep **oldest** row.
  - Non-canonical rows: mark `dead_letter` with `failure_reason=duplicate-pruned` and stamp `failed_at`.
  - Run dedupe + index creation in one migration transaction (fail whole step if duplicates remain).
- Replace read-then-insert race path with conflict-safe create:
  - Insert with conflict handling, then fetch canonical row.
  - Ensure all concurrent callers resolve to same canonical `message_id`.

Observability:
- Log migration summary: duplicate groups found, rows pruned, index created.
- Add telemetry counters for conflict-path hits and duplicate-pruned rows.

Acceptance:
- Parallel create requests for same `(receiver_id, idempotency_key)` return one canonical `message_id`.
- Exactly one deliverable row exists per key after race completion.
- Migration leaves zero duplicate `(receiver_id, idempotency_key)` pairs.

Tests:
- Concurrent insertion test (threaded DB-level) asserting one canonical row.
- Concurrent API-level test (parallel requests) asserting same message ID and single effective delivery.
- Migration test with seeded duplicates asserting oldest survives and others are `dead_letter` + `duplicate-pruned`.

---

### WS2: Dead-letter resend semantics for idempotency keys
Scope:
- `src/cli_agent_orchestrator/clients/database.py`
- `src/cli_agent_orchestrator/api/main.py`
- `docs/runtime-profile.md` and `docs/api.md` (contract notes)

Contract decision:
- For existing `(receiver_id, idempotency_key)` row in terminal state (`dead_letter` or `failed`), allow explicit requeue by resetting canonical row to `pending` and resetting retry metadata.
- For `pending`/`retrying`/`delivered`, return existing canonical row unchanged.
- Add explicit API control: `requeue_terminal_state` (boolean).

Compatibility rollout:
- Phase 1: flag defaults to `false` (legacy-safe).
- Phase 2: flip default to `true` only after clients/docs are updated and validated.
- Emergency rollback: flip default back to `false` without code rollback.

Changes:
- Implement requeue path in `create_inbox_message`.
- Reset metadata on requeue (`attempt_count`, `failure_reason`, `next_attempt_at`, `last_attempt_at`, `failed_at`).
- Preserve canonical row identity (`id`, `idempotency_key`).

Observability:
- Add counter/log for `dead_letter_requeue_attempted` and `dead_letter_requeue_applied`.
- Include flag value in request-path logs.

Acceptance:
- Dead-letter row requeue with `requeue_terminal_state=true` returns to `pending` and becomes deliverable.
- Requeue operation fully resets retry metadata.
- Legacy mode (`requeue_terminal_state=false`) preserves current behavior.
- Delivered rows remain immutable on same-key requests.

Tests:
- `create -> dead_letter -> create(same key, requeue=true)` resets metadata and status.
- `create -> delivered -> create(same key, requeue=true)` remains delivered.
- `create -> dead_letter -> create(same key, requeue=false)` stays terminal.

---

### WS3: Approval queue cleanup on terminal deletion
Scope:
- `src/cli_agent_orchestrator/services/terminal_service.py`
- `src/cli_agent_orchestrator/clients/database.py`
- session deletion paths indirectly via existing terminal deletion loops

Changes:
- In terminal deletion flow, resolve pending approvals with reason `terminal-deleted`.
- Ensure cleanup is attempted even when tmux/provider cleanup fails.
- Keep cleanup path explicit and logged; do not silently swallow failures.

Observability:
- Emit logs/counters for approval cleanup success/failure per terminal deletion.
- Add a diagnostic check for pending approvals referencing nonexistent terminals.

Acceptance:
- No `pending` approvals remain for deleted terminals after deletion/reconciliation.
- Terminal recreation does not inherit stale approval queue state.
- Cleanup executes on both happy path and partial-failure delete path.

Tests:
- Integration test: pending approval -> delete terminal -> approval resolved with `terminal-deleted`.
- Failure-path test: inject deletion substep failure and still verify approval cleanup attempt/result.

---

### WS4: Validate flow provider at creation time
Scope:
- `src/cli_agent_orchestrator/services/flow_service.py`
- flow API/CLI error propagation tests

Changes:
- Validate parsed flow provider against `PROVIDERS` during `add_flow`.
- Return actionable error including invalid provider and allowed list.

Acceptance:
- Invalid provider fails during flow creation (before persistence/execution).
- Error message is clear in both service and user-facing entrypoints.

Tests:
- Service test: invalid provider raises `ValueError` with allowed set.
- CLI/API path test: invalid provider receives actionable validation error.
- Regression test: bypass attempts cannot execute invalid provider flow.

---

### WS5: API/docs parity update
Scope:
- `docs/api.md`
- `docs/runtime-profile.md`
- (Optional) README cross-links

Changes:
- Update provider list to include all supported providers (`q_cli`, `claude_code`, `kiro_cli`, `codex`).
- Document diagnostics endpoints:
  - `GET /diagnostics/inbox/telemetry`
  - `GET /diagnostics/jsonl/gates`
  - `POST /diagnostics/jsonl/reset`
- Document approval endpoints:
  - `GET /terminals/{terminal_id}/approvals`
  - `POST /terminals/{terminal_id}/approvals/{approval_id}/ack`
- Document inbox create parameters:
  - `idempotency_key`
  - `max_attempts`
  - `requeue_terminal_state`
- Document status enum and transitions (`pending`, `retrying`, `delivered`, `failed`, `dead_letter`) including dead-letter requeue contract and defaults.

Acceptance:
- Public docs fully match runtime API contract and defaults.
- Requeue semantics (and flag default) are documented unambiguously.

Tests:
- Add API smoke checks that hit each documented endpoint and parameter path.
- Add lightweight doc checklist verification in release notes review.

---

### WS6: Expand runtime profile tests
Scope:
- `test/test_profile_config.py`

Changes:
- Add dedicated tests for each profile family:
  - JSONL rollout selectors/gates
  - inbox retry/backoff values
  - approval queue toggles
  - single-writer settings
- Add explicit precedence tests per setting family:
  - env var overrides profile value
  - profile value used when env absent
  - fallback default when both absent

Acceptance:
- Each new config family has explicit profile + precedence coverage.
- Regression in parsing/precedence for any new setting fails tests.

Tests:
- Extend current profile test module with a matrix covering all new constants.

## Execution Order
1. WS1 (idempotency safety foundation + migration)
2. WS2 (terminal-state resend semantics + flag)
3. WS3 (approval lifecycle cleanup)
4. WS4 (flow validation fail-fast)
5. WS6 (test coverage hardening)
6. WS5 (docs parity once behavior contracts are finalized)

## Rollout Plan
1. Stage WS1 migration on a copy of production-like DB; verify dedupe report and uniqueness enforcement.
2. Deploy WS1+WS3+WS4 with requeue flag default `false`.
3. Monitor telemetry/logs for duplicate-pruned counts, conflict-path rates, approval-cleanup failures.
4. Deploy WS2 contract docs and client comms.
5. Enable `requeue_terminal_state` default `true` only after compatibility sign-off.

## Verification Checklist
- `uv run pytest -q test/clients/test_database.py test/services/test_inbox_service.py test/services/test_inbox_delivery_integration.py test/orchestration/test_error_handling.py`
- `uv run pytest -q test/services/test_flow_service.py test/test_profile_config.py`
- `uv run pytest -q -m "not e2e"`
- `uv run python -m compileall -q src`
- API smoke checks (scripted):
  - concurrent idempotent create calls -> canonical row behavior
  - dead-letter requeue with flag on/off
  - approvals create/list/ack + terminal delete cleanup
  - diagnostics endpoints return expected payload shape

## Risks
- Unique-index migration can fail if duplicate cleanup logic is incomplete.
- Requeue behavior is a contract change for clients expecting immutable dead-letter history.
- Cleanup hooks in deletion paths may fail silently unless instrumented and monitored.

## Rollback Strategy
- Keep workstreams isolated so problematic behavior can be rolled back selectively.
- If WS1 migration fails, abort transaction and keep pre-migration schema/data unchanged.
- If WS2 breaks client assumptions, revert default flag to `false` immediately.
- If WS3 causes deletion regressions, disable cleanup hook and run reconciliation script separately.

## Deliverables
- Code changes across WS1-WS6.
- Updated tests proving behavior and compatibility paths.
- Updated `docs/api.md` + runtime profile docs.
- Short changelog note summarizing contract updates.
