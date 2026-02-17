# E2E Availability Notes

## Why E2E Was Unavailable
On February 17, 2026, the E2E suite skipped because the CAO API server was not running at `http://localhost:9889`.

Observed during diagnosis:
- `uv run pytest test/e2e/ -m e2e -rs` reported `Connection refused` for `/health`
- `curl http://localhost:9889/health` failed with connection refused
- no `cao-server`/uvicorn process was running

## Verification
When the server is started manually, health checks pass:

```bash
cd /home/charl/projects/ORCH-SYSTEM/CARO-FORK
uv run cao-server
# in another shell
curl -i http://localhost:9889/health
```

Expected health response:
- HTTP `200`
- body: `{"status":"ok","service":"cli-agent-orchestrator"}`

## Run E2E Successfully
1. Start CAO API server:

```bash
cd /home/charl/projects/ORCH-SYSTEM/CARO-FORK
CAO_CLAUDE_PERMISSION_MODE=bypassPermissions uv run cao-server
```

2. In another shell, run E2E tests:

```bash
cd /home/charl/projects/ORCH-SYSTEM/CARO-FORK
uv run pytest test/e2e/ -m e2e -q
```

## Notes
- E2E tests are enabled by default now.
- Health checks now retry before skipping, controlled by:
  - `CAO_E2E_HEALTH_ATTEMPTS` (default `5`)
  - `CAO_E2E_HEALTH_TIMEOUT` (default `5`)
  - `CAO_E2E_HEALTH_INTERVAL_SECONDS` (default `1`)
- E2E can target a non-default CAO server with:
  - `CAO_E2E_API_BASE_URL` (for tests)
  - `CAO_API_BASE_URL` is auto-aligned during E2E so MCP helper calls use the same endpoint.
- Session names are randomized per test run to avoid collisions with stale tmux sessions.
- Claude E2E preflight runs by default and fails fast when interactive permission prompts are detected:
  - `CAO_E2E_PREFLIGHT_ENABLED` (default `true`)
  - `CAO_E2E_PREFLIGHT_TIMEOUT` (default `30`)
  - It now also surfaces idle-timeout diagnostics with terminal output snippets
    (useful for account limit/auth/composer-state blockers).
- Provider/tool prerequisites still apply (for example, installed CLI provider and valid profile).

## Runtime Controls
You can tune E2E timing behavior without changing code:

- `CAO_E2E_REQUEST_TIMEOUT` (default `45`) for API requests in tests
- `CAO_E2E_HANDOFF_TIMEOUT` (default `180`) for MCP handoff
- `CAO_E2E_ASSIGN_COMPLETION_TIMEOUT` (default `240`) for assign completion polling
- `CAO_HANDOFF_IDLE_WAIT_SECONDS` (default `30`) for MCP handoff pre-send IDLE gating
- `CAO_HANDOFF_POST_IDLE_GRACE_SECONDS` (default `2`) extra delay before sending input
- `CAO_ASSIGN_POST_IDLE_GRACE_SECONDS` (default `2`) extra delay before first assign input
- `CAO_HANDOFF_STATUS_POLL_SECONDS` (default `1`) polling interval for handoff status checks
- `CAO_CLAUDE_PERMISSION_MODE` (server-side) to force non-interactive Claude startup
  - Recommended for e2e: `bypassPermissions`
- `CAO_CLAUDE_DANGEROUS_SKIP_PERMISSIONS` (server-side, optional) to fully bypass permissions

Example bounded diagnostic run:

```bash
CAO_E2E_REQUEST_TIMEOUT=20 \
CAO_E2E_HANDOFF_TIMEOUT=30 \
CAO_E2E_ASSIGN_COMPLETION_TIMEOUT=45 \
uv run pytest test/e2e/ -m e2e -q -rs
```

On February 17, 2026 this bounded run produced:
- `2 failed, 3 passed`
- Failures were the two long-running orchestration tests timing out before completion.

## Deep-Dive Findings (February 17, 2026)
Running the two failing tests with default timeouts identified two concrete blockers:

1. Shared-server contention on port `9889`
- Tests were hitting an already running shared CAO uvicorn instance.
- Under concurrent orchestration load, session creation and terminal startup became slow/erratic.
- Evidence: repeated `Codex initialization timed out after 60 seconds` in
  `/home/charl/.aws/cli-agent-orchestrator/logs/cao_2026-02-17_02-10-46.log` (for example line `101`, line `176`, line `279`).

2. Claude terminal blocked on interactive permission prompt
- In `test_e2e_assign_and_poll_until_completed`, the worker never reached `completed` because Claude was waiting for interactive permission selection.
- Evidence in terminal log:
  `/home/charl/.aws/cli-agent-orchestrator/logs/terminal/039ea536.log:2`
  shows `bypass permissions on (shift+tab to cycle)`.

Implication:
- `handoff` can fail at the pre-send `IDLE` gate if startup exceeds the hard 30-second window.
- `assign` can fail the 240-second completion wait when the worker is blocked on an interactive prompt.
