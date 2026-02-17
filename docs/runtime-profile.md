# Runtime Profile (`profile.json`)

CAO supports a user-level runtime profile to set durable defaults without exporting many env vars.

- Default path: `~/.aws/cli-agent-orchestrator/profile.json`
- Override path: `CAO_PROFILE_PATH=/absolute/path/profile.json`
- Precedence: environment variables override profile values.

## Example

```json
{
  "defaults": {
    "provider": "codex",
    "working_directory": "/home/charl/projects",
    "status_source": "hybrid"
  },
  "jsonl": {
    "watch": {
      "enabled": true
    },
    "paths": {
      "claude_root": "~/.claude/projects",
      "codex_root": "~/.codex/sessions"
    },
    "rollout": {
      "terminal_ids": ["a1b2c3d4"],
      "session_names": ["cao-rollout"]
    },
    "gates": {
      "fallback_threshold": 0.05,
      "disagreement_threshold": 0.01,
      "watcher_error_threshold": 0.001,
      "tmux_comparison_enabled": false,
      "enforce_tmux_disagreement": false
    }
  },
  "inbox": {
    "max_delivery_attempts": 5,
    "retry": {
      "base_seconds": 2,
      "multiplier": 2.0,
      "max_seconds": 60
    },
    "requeue_terminal_state_default": false
  },
  "approvals": {
    "enabled": true,
    "prompt_tail_lines": 25
  },
  "single_writer": {
    "enforced": true,
    "allow_override": false,
    "lockfile": "~/.aws/cli-agent-orchestrator/db/server-writer.lock"
  }
}
```

## What It Controls

- Launch defaults (`provider`, `working_directory`) used by `cao launch` and API terminal creation.
- JSONL status source and rollout gate thresholds.
- JSONL watcher enablement/path roots used by server-side file observers.
- Inbox retry/backoff defaults, including dead-letter promotion.
- Approval queue behavior for `WAITING_USER_ANSWER` states.
- Single-writer lock behavior for CAO API instances sharing the same DB/watchers.

## Related Endpoints

- `POST /terminals/{receiver_id}/inbox/messages`
  - Supports `idempotency_key`, `max_attempts`, and `requeue_terminal_state`.
- `GET /terminals/{terminal_id}/approvals`
- `POST /terminals/{terminal_id}/approvals/{approval_id}/ack`
