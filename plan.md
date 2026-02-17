# CAO JSONL Cutover Plan (1A / 2A / 3A / 4A)

## Decisions Locked
- `1A`: Persisted deterministic mapping is sticky. Do not demote on soft tmux-text mismatch.
- `2A`: Canary gates are rollout-scoped, not global.
- `3A`: Single CAO writer is enforced for shared DB/watchers.
- `4A`: Claude hooks are conditional escalation only; Codex stays non-hook and uses startup mapping/launcher contract.

## Goals
- Keep inbox delivery and status detection stable while moving status/source of truth to JSONL.
- Remove false demotions caused by prompt/screen-text drift.
- Avoid noisy canary failures caused by unrelated terminals/sessions.
- Detect when heuristics are no longer sufficient and escalation is required.

## Success Criteria (Rollout Scope)
All criteria must pass for 3 consecutive windows:
1. Mapping stability:
   - `unmapped_rollout_terminals == 0`
   - `mapping_demotions == 0`
2. Delivery reliability:
   - `deliveries_succeeded / delivery_attempts >= 0.99`
   - `delivery_attempts >= 30` total across the 3 windows
3. Runtime health:
   - `watcher_error_rate == 0`
   - `fallback_trigger_rate < 0.02`
4. Activity sufficiency:
   - `rollout_jsonl_status_checks >= 100` total across the 3 windows
   - `rollout_trigger_jsonl_events >= 20` total across the 3 windows
5. Scope acceptance:
   - Codex `WAITING_USER_ANSWER` remains unsupported in phase-1 JSONL mode.

## Hook Escalation Conditions (Claude Only)
Switch to Claude SessionStart hook path if any condition holds for 2 consecutive windows:
1. `mapping_demotions > 0`
2. `fallback_trigger_rate >= 0.05`
3. `startup_deterministic_mapping_success < 0.99`
4. repeated `claude_ambiguous_parent_sessions` on rollout terminals

Codex path:
- No hook dependency in this phase.
- Use deterministic startup mapping/launcher contract.
- Keep Codex in hybrid for terminals that cannot map deterministically.

## Implementation Scope

### A) Sticky Mapping + Hard Invalidation Rules
- Keep persisted mapping (`provider_session_id`, `provider_log_path`) authoritative when file exists and IDs are coherent.
- Remove soft invalidation based only on tmux user-text mismatch.
- Only invalidate persisted mapping on hard evidence:
  - mapped JSONL file missing
  - mapped session ID malformed/mismatch with file identity
  - terminal/session recreation explicitly resetting mapping
- Add telemetry counters:
  - `mapping_promotions`
  - `mapping_demotions`
  - `mapping_invalidations` (+ reason counts)
  - startup mapping attempts/successes

### B) Rollout-Scoped Gates and Telemetry
- Add rollout scope selectors:
  - `CAO_JSONL_ROLLOUT_TERMINAL_IDS`
  - `CAO_JSONL_ROLLOUT_SESSION_NAMES`
- Gate evaluation uses rollout terminals only for:
  - unmapped terminal checks
  - fallback/comparison/disagreement rates
  - watcher-error impact
- Keep global counters for ops visibility, add rollout-specific counters for decisioning.

### C) Single-Writer Enforcement
- Add lockfile-based writer ownership at API startup.
- Default: startup fails fast if lock already held.
- Add optional override env for manual debugging only.
- Persist owner metadata (pid/started_at) in lockfile contents for diagnostics.

### D) Diagnostics + Verifier Updates
- Expose rollout scope and escalation condition status in gate payload.
- Verifier prefers rollout counters when available.
- Keep tmux disagreement advisory by default.

## Execution Plan
1. Patch constants/config surface (scope + lock + thresholds).
2. Patch JSONL status engine for sticky mapping + new telemetry.
3. Patch inbox telemetry/gates for rollout-scoped decisioning.
4. Patch API startup for single-writer lock.
5. Patch verifier script to consume rollout counters.
6. Add/adjust unit tests for sticky mapping and rollout gate behavior.
7. Validate with canary windows and artifact capture.

## Validation Artifacts
- `work/canary-snapshots/cutover-run*.json`
- `work/canary-snapshots/live-observer-*.log`
- `/diagnostics/jsonl/gates`
- `/diagnostics/inbox/telemetry`
