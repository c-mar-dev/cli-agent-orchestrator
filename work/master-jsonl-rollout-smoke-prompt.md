You are the master orchestrator. Run a focused JSONL rollout smoke test against the existing patched CAO server and report strict pass/fail.

Automation:
- Script implementation: `ORCH-SYSTEM/CARO-FORK/scripts/run_master_jsonl_rollout_smoke.py`
- Run: `cd ORCH-SYSTEM/CARO-FORK && uv run python scripts/run_master_jsonl_rollout_smoke.py --base-url http://127.0.0.1:9893`

Why this drill exists:
- CAO's tmux/ANSI regex parsing has historically been fragile and breaks silently when prompt rendering changes.
- This rollout validates that structured JSONL status detection is now reliable enough for real orchestration behavior.
- The key risk is false readiness or stuck processing states causing missed/delayed inbox delivery.
- We are proving that rollout-scoped terminals can be trusted on JSONL status while still in hybrid safety mode.

What this drill must prove:
- JSONL watcher is active and receiving events.
- The rollout terminal reaches usable JSONL mapping confidence (`medium` or `high`) and serves status from `jsonl`.
- Claude waiting behavior is detectable (`waiting_user_answer` preferred; strict content-based fallback accepted).
- Final decision is evidence-based, with explicit blocker reasons if anything fails.

Read these references before execution:
- Cutover plan and locked decisions: `ORCH-SYSTEM/CARO-FORK/plan.md`
- Verification thresholds and go/no-go framing: `ORCH-SYSTEM/docs-for-developing-orch-system-itself/cao-jsonl-cutover-verification-plan.md`
- ORCH integration behavior for waiting states and diagnostics usage: `ORCH-SYSTEM/docs/specs/jsonl-delivery-after-remediation.md`
- API endpoints and watcher startup wiring: `ORCH-SYSTEM/CARO-FORK/src/cli_agent_orchestrator/api/main.py`
- Rollout-scoped telemetry/gates and escalation logic: `ORCH-SYSTEM/CARO-FORK/src/cli_agent_orchestrator/services/inbox_service.py`
- JSONL mapping/status engine behavior: `ORCH-SYSTEM/CARO-FORK/src/cli_agent_orchestrator/parsing/jsonl_status_engine.py`

Context and constraints:
- CAO API base: http://127.0.0.1:9893
- Do not start/stop/restart CAO.
- Rollout session name: `cao-jsonl-rollout`
- Rollout mode is hybrid + JSONL watcher; do not change env vars.
- Treat deterministic mapping as required for this rollout terminal.
- Do not use Codex hook assumptions; Claude hook path is escalation-only.

Run this exact procedure:
1) Preflight health and watcher
- GET /health must return status ok.
- GET /diagnostics/inbox/telemetry must show:
  - jsonl_watch_enabled = true
  - jsonl_watcher_active = true
- If either fails, stop and return FAIL with reason `watcher_not_ready`.

2) Resolve rollout terminal
- GET /sessions/cao-jsonl-rollout
- If session missing, create via POST /sessions with:
  - provider=claude_code
  - agent_profile=pipe-orch
  - session_name=cao-jsonl-rollout
  - working_directory=/home/charl/projects/ORCH-SYSTEM
- Capture terminal_id from the session.

3) Stimulate waiting state
- POST /terminals/{terminal_id}/input with message query param:
  `Ask me exactly one multiple-choice question (3 options), then stop and wait for my answer.`
- Poll GET /terminals/{terminal_id} every 2s for up to 90s.
- Success condition A: status becomes `waiting_user_answer`.
- Success condition B (fallback): output clearly contains a direct question with 3 options, even if status is `completed`.
- Hard failure: status reaches `error`.

4) Confirm JSONL mapping/source
- During polling, require eventual:
  - status_source = `jsonl`
  - mapping_confidence in {`medium`, `high`}
- If never reached within timeout, FAIL with reason `jsonl_mapping_not_reliable`.

5) Collect diagnostics snapshot
- GET /diagnostics/inbox/telemetry
- GET /diagnostics/jsonl/gates
- Return compact JSON report containing:
  - why/what summary in 3-5 lines (plain language)
  - preflight result
  - terminal_id
  - observed statuses over time
  - first timestamp where status_source became jsonl
  - final mapping_confidence
  - whether waiting condition (A or B) passed
  - gate blockers (if any)
  - final PASS/FAIL and reason codes

Decision policy:
- PASS only if preflight passes, waiting condition passes, and JSONL mapping/source condition passes.
- Otherwise FAIL with explicit blocker reasons and the exact step that failed.
