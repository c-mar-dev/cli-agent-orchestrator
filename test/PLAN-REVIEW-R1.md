# Plan Review (Round 1): `immutable-sleeping-walrus.md`

## Verdict
The plan is substantially improved, but it is still **not implementation-ready**. There are remaining factual mismatches with current code paths, plus a few fixture-scope issues that will cause brittle or failing tests.

## Findings (ordered by severity)

### 1. High: `_handoff_impl` “exception after creation keeps terminal_id” is incorrect
- Plan claim: `/_handoff_impl exception after creation → HandoffResult(success=False, terminal_id=set)/` (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:113`, `/home/charl/.claude/plans/immutable-sleeping-walrus.md:241`).
- Actual code: `except` always returns `terminal_id=None` in `src/cli_agent_orchestrator/mcp_server/server.py:212` and `src/cli_agent_orchestrator/mcp_server/server.py:214`.
- Impact: those planned tests will fail unless code is changed first.

### 2. High: timeout cleanup behavior is misdocumented (no `/exit` on timeout)
- Plan claim: timeout path “currently sends /exit even on timeout” (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:115`, `/home/charl/.claude/plans/immutable-sleeping-walrus.md:242`).
- Actual control flow: timeout returns early at `src/cli_agent_orchestrator/mcp_server/server.py:181` and `src/cli_agent_orchestrator/mcp_server/server.py:184`; `/exit` is only sent on success path at `src/cli_agent_orchestrator/mcp_server/server.py:200`.
- Impact: the planned assertion is backwards; this is a real leak risk and should be tested as “no cleanup attempt currently occurs.”

### 3. High: `disable_jsonl` fixture patch list is incorrect/incomplete
- Plan says patch every module importing `CAO_JSONL_ENABLED`, including `inbox_service` (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:75`).
- `inbox_service` does not import `CAO_JSONL_ENABLED` (`src/cli_agent_orchestrator/services/inbox_service.py:24`).
- `jsonl_status_engine` does import it (`src/cli_agent_orchestrator/parsing/jsonl_status_engine.py:25`) and gates behavior on it (`src/cli_agent_orchestrator/parsing/jsonl_status_engine.py:204`), but is not in the plan list.
- Impact: fixture may patch a non-existent attribute and miss a real import site.

### 4. High: P3 says “with FakeProvider” but omits `fake_provider_manager`
- P3 description explicitly says “with FakeProvider” (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:123`).
- P3 fixture list omits `fake_provider_manager` (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:124`).
- Without provider mocking, `terminal_service.get_output(..., mode=last)` depends on real provider parsing (`src/cli_agent_orchestrator/services/terminal_service.py:231`) and will not be deterministic from in-memory fake pane content unless you fully emulate provider transcript formats.
- Impact: these tests will be flaky or fail for the wrong reasons.

### 5. Medium: `patch_async_sleep` can accidentally break API lifespan tests
- Plan says replace `asyncio.sleep` globally (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:77`).
- API background daemons run infinite loops with `asyncio.sleep` in `src/cli_agent_orchestrator/api/main.py:96`, `src/cli_agent_orchestrator/api/main.py:114`, `src/cli_agent_orchestrator/api/main.py:117`, `src/cli_agent_orchestrator/api/main.py:127`.
- Impact: if patched globally while `TestClient` lifespan is active, loops can spin hot. Patch `cli_agent_orchestrator.mcp_server.server.asyncio.sleep` only.

### 6. Medium: P7 includes a persistence assertion that route-unit tests cannot prove
- P7 says route tests will use service-level mocking (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:186`).
- It also says to verify “message still created (DB insert succeeded)” when delivery fails (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:201`).
- Endpoint implementation creates then delivers in one `try` (`src/cli_agent_orchestrator/api/main.py:447`, `src/cli_agent_orchestrator/api/main.py:448`).
- Impact: with service mocks you can only verify call ordering/response, not durable DB state. Move DB-persistence assertion to integration tests (P4/P3).

### 7. Medium: `fake_provider_manager` patch scope should include API module for consistency
- Plan says patch provider manager “into services” (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:69`).
- API `/terminals/{id}/exit` uses its own imported singleton `provider_manager` (`src/cli_agent_orchestrator/api/main.py:35`, `src/cli_agent_orchestrator/api/main.py:411`).
- Impact: mixed provider-manager singletons across service/API layers can produce inconsistent behavior in integration tests.

### 8. Low: P9 still lists tests already covered
- Already covered: provider on-demand creation (`test/providers/test_provider_manager_unit.py:38`) and cleanup (`test/providers/test_provider_manager_unit.py:56`).
- Already covered: Codex initialization waits for shell and sends `codex` (`test/providers/test_codex_provider_unit.py:20`, `test/providers/test_codex_provider_unit.py:32`).
- Impact: low ROI duplication if re-added.

### 9. Low: baseline count in context is stale
- Plan says `~249 tests, 16 files` (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:5`).
- Current collection in this workspace is `250 tests` across `17` `test_*.py` files.
- Impact: minor, but baseline statements should be regenerated before planning coverage deltas.

## Per-file Evaluation

### P1 `test/clients/test_database.py`
- Meaningful and high value. No existing direct DB coverage.
- Mocking boundary is correct (`nothing mocked`).
- Add one more case: duplicate flow name (PK violation via `create_flow`) in `src/cli_agent_orchestrator/clients/database.py:393`.

### P2 `test/mcp_server/test_server.py`
- Strong target area, currently untested.
- Fix the two incorrect expectations from Findings #1 and #2.
- Keep direct patch of `wait_until_terminal_status` (correct per `src/cli_agent_orchestrator/utils/terminal.py:91`).

### P3 `test/orchestration/test_handoff_flow.py`
- Valuable integration target.
- Must include `fake_provider_manager` (or equivalent deterministic provider stubbing) to align with stated approach.
- Ensure env setup for `send_message` sender context (`src/cli_agent_orchestrator/mcp_server/server.py:143`).

### P4 `test/services/test_inbox_delivery_integration.py`
- Good tests for real DB + state transitions.
- Avoid duplicating telemetry-unit coverage already present in `test/services/test_inbox_service.py:11`.
- API immediate-delivery behavior test is appropriate (`src/cli_agent_orchestrator/api/main.py:447`).

### P5 `test/services/test_session_lifecycle.py`
- Good and currently missing.
- `kill_session=False` cleanup assertion is valid because return value is ignored (`src/cli_agent_orchestrator/services/session_service.py:60`).

### P6 `test/services/test_terminal_service_integration.py`
- Good scope and corrected rollback path (`src/cli_agent_orchestrator/services/terminal_service.py:135`).
- Not redundant with existing `test/services/test_terminal_service.py` (that file is mostly working-directory + startup mapping).

### P7 `test/api/test_api_endpoints.py`
- Gap-focused list is mostly right given current API test coverage in:
- `test/api/test_terminals.py:19`
- `test/api/test_inbox_messages.py:52`
- `test/api/test_diagnostics.py:16`
- Keep it route-layer unit style; move DB persistence claims to integration tests.

### P8 `test/services/test_flow_service.py`
- High value and currently absent.
- Good coverage of timeout/shape failures (`src/cli_agent_orchestrator/services/flow_service.py:167`, `src/cli_agent_orchestrator/services/flow_service.py:181`).

### P9 `test/providers/test_provider_lifecycle.py`
- Should be narrowed further to only true gaps.
- Keep only Claude command construction with profile/system prompt (`src/cli_agent_orchestrator/providers/claude_code.py:50`) if still untested.

### P10 `test/orchestration/test_error_handling.py`
- Valuable direction.
- Update handoff expectations to actual current behavior (no timeout cleanup, `terminal_id=None` in broad exception return).

### P11 `test/e2e/test_e2e_orchestration.py`
- Fine as optional/manual. Keep strict skip guards and robust cleanup.

## Mock Boundary + Fixture Design Assessment

### `FakeTmuxClient`
- Design is generally sound and aligned with `TmuxClient` public surface (`src/cli_agent_orchestrator/clients/tmux.py:44`, `src/cli_agent_orchestrator/clients/tmux.py:76`, `src/cli_agent_orchestrator/clients/tmux.py:108`, `src/cli_agent_orchestrator/clients/tmux.py:126`, `src/cli_agent_orchestrator/clients/tmux.py:155`, `src/cli_agent_orchestrator/clients/tmux.py:177`, `src/cli_agent_orchestrator/clients/tmux.py:194`, `src/cli_agent_orchestrator/clients/tmux.py:207`, `src/cli_agent_orchestrator/clients/tmux.py:215`, `src/cli_agent_orchestrator/clients/tmux.py:237`, `src/cli_agent_orchestrator/clients/tmux.py:262`).
- Ensure deterministic `get_history` semantics for repeated calls (wait-for-shell and provider status loops depend on stable outputs).

### `FakeProvider`
- Good abstraction for state-machine tests.
- Keep `extract_last_message_from_script` deterministic since handoff success reads `/output?mode=last` (`src/cli_agent_orchestrator/mcp_server/server.py:192`, `src/cli_agent_orchestrator/services/terminal_service.py:235`).

### `in_memory_db`
- `StaticPool` + `check_same_thread=False` is the right approach for shared in-memory access in threaded/lifespan scenarios.

## Required Plan Corrections Before Implementation

1. Fix handoff error/timeout expectations in P2 and P10 to match current `server.py` control flow.
2. Correct `disable_jsonl`: remove `inbox_service` target, add `parsing.jsonl_status_engine`.
3. Add `fake_provider_manager` to P3 fixture list if P3 is truly FakeProvider-based.
4. Scope `patch_async_sleep` to `cli_agent_orchestrator.mcp_server.server.asyncio.sleep` only.
5. Keep P7 as route-unit tests; move DB-persistence assertions to P3/P4 integration tests.
6. Trim P9 duplicated tests already covered in existing provider unit files.
