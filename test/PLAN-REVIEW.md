# Plan Review: `immutable-sleeping-walrus.md`

## Verdict
The plan is directionally strong (it targets real coverage gaps in `mcp_server`, DB functions, session/flow services), but it is **not implementation-ready** as written. Several assumptions are stale or incorrect, and some proposed tests are impossible or internally inconsistent with current code paths.

## Findings (ordered by severity)

### 1. `test/orchestration/test_handoff_flow.py` approach will not work without additional patching
- The plan proposes a `RequestsBridge` that maps `requests.get/post/delete` into `TestClient` calls (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:100`).
- `_handoff_impl` does not only use `requests`; it also calls `wait_until_terminal_status(...)` (`src/cli_agent_orchestrator/mcp_server/server.py:167`, `src/cli_agent_orchestrator/mcp_server/server.py:181`).
- `wait_until_terminal_status` uses `httpx.get(...)` directly (`src/cli_agent_orchestrator/utils/terminal.py:91`).
- Result: tests will still attempt real network calls to `http://localhost:9889` unless `wait_until_terminal_status` (or `httpx`) is patched too.

### 2. `_send_to_inbox` endpoint in plan is wrong
- Plan says `_send_to_inbox` should call `POST /inbox/messages` (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:91`).
- Actual implementation calls `POST /terminals/{receiver_id}/inbox/messages` (`src/cli_agent_orchestrator/mcp_server/server.py:147`).
- API route confirms this path (`src/cli_agent_orchestrator/api/main.py:441`).

### 3. One rollback case in P6 is impossible as specified
- Plan: “Create terminal rollback on error: session killed if `create_window` fails” (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:155`).
- `create_window` is only used when `new_session=False` (`src/cli_agent_orchestrator/services/terminal_service.py:64`).
- Rollback `kill_session` runs only when `new_session=True` (`src/cli_agent_orchestrator/services/terminal_service.py:135`).
- So this exact scenario cannot exercise the rollback branch.

### 4. `sqlite:///:memory:` fixture is risky with this app lifecycle
- Plan uses in-memory DB (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:53`) and also proposes a real `FastAPI TestClient` (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:59`).
- App lifespan starts background threaded work (`asyncio.to_thread(...)`) (`src/cli_agent_orchestrator/api/main.py:153`, `src/cli_agent_orchestrator/api/main.py:122`).
- With SQLite in-memory DB, connection/thread behavior can split state unless the engine is configured for shared connection semantics.
- This is likely to cause flaky failures once integration tests run through lifespan/background tasks.

### 5. `disable_jsonl` is underspecified and likely ineffective if done via env vars only
- Plan says “Sets `CAO_JSONL_ENABLED = False`” (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:63`), but many modules import this constant at module load time (`src/cli_agent_orchestrator/services/terminal_service.py:15`, `src/cli_agent_orchestrator/providers/codex.py:8`, `src/cli_agent_orchestrator/providers/claude_code.py:8`).
- If this fixture only mutates environment variables, it will not propagate.
- It must monkeypatch module-level constants where used.

### 6. `send_message queues to inbox` assumption is incomplete
- Plan asserts a queueing behavior in P3 (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:106`).
- API immediately attempts delivery right after message creation (`src/cli_agent_orchestrator/api/main.py:447`, `src/cli_agent_orchestrator/api/main.py:448`).
- Whether message remains `PENDING` depends on receiver readiness; queueing is not unconditional.

### 7. Plan baseline is stale; overlap/redundancy is under-accounted
- Plan claims ~150 tests (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:5`). Current suite collects **249 tests** (`uv run pytest --collect-only -q test`).
- Some proposed files duplicate existing coverage:
- Provider init/manager tests already exist (`test/providers/test_q_cli_unit.py:28`, `test/providers/test_codex_provider_unit.py:23`, `test/providers/test_provider_manager_unit.py:38`).
- Working-directory behavior already has coverage across tmux/service/api (`test/providers/test_tmux_working_directory.py:92`, `test/services/test_terminal_service.py:16`, `test/api/test_terminals.py:21`).

## Per-file evaluation

### P1 `test/clients/test_database.py`
- High value and mostly correct.
- Add coverage for `_ensure_terminal_columns()` migration/backfill behavior (`src/cli_agent_orchestrator/clients/database.py:80`) since this is production-critical and currently untested.
- “Nothing mocked” is good.

### P2 `test/mcp_server/test_server.py`
- High value and strongly needed (`mcp_server/server.py` currently untested).
- Fix `_send_to_inbox` endpoint assertion as noted above.
- Add explicit case for failure **after terminal creation** in `_handoff_impl` to document returned `terminal_id` behavior (currently broad exception returns `terminal_id=None`) (`src/cli_agent_orchestrator/mcp_server/server.py:212`).

### P3 `test/orchestration/test_handoff_flow.py`
- Good target area, but current bridge strategy is incomplete because of `httpx` polling path.
- Needs either:
- patch `wait_until_terminal_status` directly, or
- bridge `httpx` too, or
- run against real API server process.

### P4 `test/services/test_inbox_delivery_integration.py`
- Good and meaningful.
- Large overlap with existing telemetry/watcher tests in `test/services/test_inbox_service.py`.
- Focus additions on true integration boundaries (DB + provider state + delivery transitions) instead of duplicating telemetry assertions already covered.

### P5 `test/services/test_session_lifecycle.py`
- Valuable; current `session_service` is largely uncovered.
- Consider a case where `kill_session` returns `False` after `session_exists=True` to verify DB/provider cleanup semantics (`src/cli_agent_orchestrator/services/session_service.py:60`).

### P6 `test/services/test_terminal_service_integration.py`
- Valuable with one correction: replace impossible rollback case (`create_window` failure + session kill) with rollback cases reachable in `new_session=True` flow (e.g., provider init failure after session creation).

### P7 `test/api/test_api_integration.py`
- Mixed quality as described.
- The phrase “integration” conflicts with “service-level mocking” (`/home/charl/.claude/plans/immutable-sleeping-walrus.md:160`). Pick one style per file.
- Avoid duplicating `test/api/test_terminals.py`, `test/api/test_inbox_messages.py`, `test/api/test_diagnostics.py` unless replacing them.

### P8 `test/services/test_flow_service.py`
- High value; current flow service coverage is absent.
- Missing important cases:
- invalid `provider` in flow metadata (currently no validation despite `PROVIDERS` import) (`src/cli_agent_orchestrator/services/flow_service.py:22`, `src/cli_agent_orchestrator/services/flow_service.py:72`),
- script timeout from `subprocess.run(..., timeout=30)` (`src/cli_agent_orchestrator/services/flow_service.py:167`),
- invalid JSON shape (`execute`/`output` not proper types) (`src/cli_agent_orchestrator/services/flow_service.py:181`).

### P9 `test/providers/test_provider_lifecycle.py`
- Mostly redundant with existing unit tests; low ROI.
- Keep only gaps that are truly missing (if any) and avoid re-testing already-verified initialization/status basics.

### P10 `test/orchestration/test_error_handling.py`
- Good direction.
- Ensure failures verify side effects, not only `success=False` flags (e.g., no silent orphan resources, message status transitions to `FAILED`).

### P11 `test/orchestration/test_working_directory.py`
- Partial duplication of existing tests.
- The MCP `_create_terminal` cwd inheritance cases are worthwhile and should be merged into P2 rather than separate file.

### P12 `test/e2e/test_e2e_orchestration.py`
- Useful for manual validation but likely flaky/slow for regular workflows.
- Keep opt-in with strict skip guards and session cleanup.

## Mock boundary review (`FakeTmuxClient` / `FakeProvider`)

### `FakeTmuxClient`
- Concept is sound for service-level integration tests.
- Must accurately emulate behavior used by services/providers:
- method signatures (including optional `tail_lines`),
- session existence checks and errors,
- `list_sessions()` shape (`id`, `name`, `status`) (`src/cli_agent_orchestrator/clients/tmux.py:155`),
- `get_pane_working_directory()` behavior.
- Plan says “all 12 methods,” but public surface appears to be 11 major operations; document exact interface to avoid drift.

### `FakeProvider`
- Useful for orchestration state-machine tests.
- Should include deterministic `get_status()` sequencing and configurable failure injection.
- If used with inbox tests, include `uses_jsonl_status()`/`get_tmux_status()` behavior where telemetry logic depends on them (`src/cli_agent_orchestrator/services/inbox_service.py:566`, `src/cli_agent_orchestrator/services/inbox_service.py:572`).

## Additional missing tests worth adding
- API inbox endpoint transactional behavior when immediate delivery fails after DB insert (`src/cli_agent_orchestrator/api/main.py:447`, `src/cli_agent_orchestrator/api/main.py:448`).
- `_handoff_impl` timeout/error cleanup policy (terminal leak risk if no `/exit`) (`src/cli_agent_orchestrator/mcp_server/server.py:184`, `src/cli_agent_orchestrator/mcp_server/server.py:212`).
- `init_db()`/`_ensure_terminal_columns()` migration behavior for pre-existing DBs (`src/cli_agent_orchestrator/clients/database.py:74`, `src/cli_agent_orchestrator/clients/database.py:80`).

## Recommended adjustments before implementation
1. Fix incorrect assumptions in P2/P3/P6 (endpoint path, `httpx` polling, rollback case).
2. Reclassify tests as either true integration (real services+DB+fakes at tmux boundary) or unit (mocks), not both.
3. De-duplicate against existing provider/working-directory/API tests; prioritize uncovered modules (`mcp_server`, `flow_service`, `session_service`, DB migrations).
4. Make fixtures explicit about module-level patch targets (not env-only) and thread-safe DB engine setup when using app lifespan.
5. Relax runtime expectation (`<30s`) or reduce test scope; current proposal size is unlikely to stay under that budget.

