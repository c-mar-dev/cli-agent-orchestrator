# Plan Review (Round 2): `immutable-sleeping-walrus.md`

## Verdict
The plan is very close, but there is still **one implementation-blocking factual mismatch**.

## Findings (ordered by severity)

### 1. High: P7 inbox error-path expectation is incorrect
- Plan claim: in P7, "Create inbox message when delivery raises: endpoint still returns success" (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:201`).
- Actual code: the endpoint wraps both `create_inbox_message(...)` and `check_and_send_pending_messages(...)` in one `try`, and any exception from delivery returns HTTP 500 (`src/cli_agent_orchestrator/api/main.py:447`, `src/cli_agent_orchestrator/api/main.py:448`, `src/cli_agent_orchestrator/api/main.py:460`).
- Impact: that planned assertion will fail. The persisted-DB side effect may still exist, but response success does not.

## Re-check of Round 0 / Round 1 corrections
- Re-validated and correct in current plan/code: `_send_to_inbox` endpoint path, handoff timeout `/exit` behavior, broad `except` returning `terminal_id=None`, `disable_jsonl` patch targets, `patch_async_sleep` scope, P3 fixture scope, and implementation order dependencies.

## Required correction
1. Update P7’s error-path case to expect HTTP 500 when delivery raises, and keep DB-persistence verification in integration tests (P3/P4).
