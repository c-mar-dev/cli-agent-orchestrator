"""Constants for CLI Agent Orchestrator application."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Set

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.utils.profile import (
    get_profile_path,
    get_profile_value,
    load_profile,
)

# Session configuration
SESSION_PREFIX = "cao-"

# Available providers (derived from enum)
PROVIDERS = [p.value for p in ProviderType]

# Application directories
CAO_HOME_DIR = Path.home() / ".aws" / "cli-agent-orchestrator"
DB_DIR = CAO_HOME_DIR / "db"
LOG_DIR = CAO_HOME_DIR / "logs"
TERMINAL_LOG_DIR = LOG_DIR / "terminal"
TERMINAL_LOG_DIR.mkdir(parents=True, exist_ok=True)

# Runtime profile configuration (env still has priority).
CAO_PROFILE_PATH = get_profile_path(CAO_HOME_DIR)
CAO_PROFILE = load_profile(CAO_HOME_DIR)


# Terminal log configuration
INBOX_POLLING_INTERVAL = 5  # Seconds between polling for log file changes
INBOX_SERVICE_TAIL_LINES = 5  # Number of lines to check in get_status for inbox service
# Default number of lines to capture from tmux history when callers do not pass a tail size.
TMUX_HISTORY_LINES = 2000

# Cleanup configuration
RETENTION_DAYS = 14  # Days to keep terminals, messages, and logs

AGENT_CONTEXT_DIR = CAO_HOME_DIR / "agent-context"

# Agent store directories
LOCAL_AGENT_STORE_DIR = CAO_HOME_DIR / "agent-store"

# Q CLI directories
Q_AGENTS_DIR = Path.home() / ".aws" / "amazonq" / "cli-agents"

# Kiro CLI directories
KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"


# Server configuration
SERVER_HOST = "localhost"
SERVER_PORT = 9889
SERVER_VERSION = "0.1.0"
API_BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
CORS_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]


# Typed config helpers


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_int(value: Any) -> int:
    return int(str(value).strip())


def _parse_float(value: Any) -> float:
    return float(str(value).strip())


def _parse_csv_set(value: str) -> Set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _parse_set_value(value: Any) -> Set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return _parse_csv_set(value)
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return set()


def _parse_optional_path(value: Any) -> Optional[str]:
    text = str(value).strip()
    return text or None


def _resolve_setting(
    env_name: str,
    profile_path: Iterable[str],
    default: Any,
    parser: Callable[[Any], Any],
) -> Any:
    raw_env = os.getenv(env_name)
    if raw_env is not None:
        return parser(raw_env)
    profile_value = get_profile_value(CAO_PROFILE, profile_path, default=None)
    if profile_value is not None:
        return parser(profile_value)
    return default


def _resolve_provider_default() -> str:
    configured = _resolve_setting(
        "CAO_DEFAULT_PROVIDER",
        ("defaults", "provider"),
        ProviderType.Q_CLI.value,
        lambda v: str(v).strip(),
    )
    if configured not in PROVIDERS:
        return ProviderType.Q_CLI.value
    return configured


DEFAULT_PROVIDER = _resolve_provider_default()

# Launch defaults
DEFAULT_WORKING_DIRECTORY = _resolve_setting(
    "CAO_DEFAULT_WORKING_DIRECTORY",
    ("defaults", "working_directory"),
    None,
    _parse_optional_path,
)

# Database configuration
DATABASE_FILE = DB_DIR / "cli-agent-orchestrator.db"
DATABASE_URL = f"sqlite:///{DATABASE_FILE}"


# JSONL status source configuration
CAO_STATUS_SOURCE = _resolve_setting(
    "CAO_STATUS_SOURCE",
    ("defaults", "status_source"),
    "hybrid",
    lambda v: str(v).strip().lower(),
)
CAO_JSONL_ENABLED = CAO_STATUS_SOURCE in ("jsonl", "hybrid")
CAO_JSONL_WATCH_ENABLED = _resolve_setting(
    "CAO_JSONL_WATCH",
    ("jsonl", "watch", "enabled"),
    True,
    _parse_bool,
)
CAO_JSONL_CLAUDE_ROOT = Path(
    _resolve_setting(
        "CAO_JSONL_CLAUDE_ROOT",
        ("jsonl", "paths", "claude_root"),
        str(Path.home() / ".claude" / "projects"),
        lambda v: str(v),
    )
).expanduser()
CAO_JSONL_CODEX_ROOT = Path(
    _resolve_setting(
        "CAO_JSONL_CODEX_ROOT",
        ("jsonl", "paths", "codex_root"),
        str(Path.home() / ".codex" / "sessions"),
        lambda v: str(v),
    )
).expanduser()
CAO_JSONL_TAIL_LINES = _resolve_setting(
    "CAO_JSONL_TAIL_LINES",
    ("jsonl", "tail_lines"),
    2000,
    _parse_int,
)
CAO_JSONL_MAPPING_GRACE_SECONDS = _resolve_setting(
    "CAO_JSONL_MAPPING_GRACE_SECONDS",
    ("jsonl", "mapping_grace_seconds"),
    180,
    _parse_int,
)
CAO_JSONL_STARTUP_MAP_TIMEOUT_SECONDS = _resolve_setting(
    "CAO_JSONL_STARTUP_MAP_TIMEOUT_SECONDS",
    ("jsonl", "startup_map", "timeout_seconds"),
    15,
    _parse_int,
)
CAO_JSONL_STARTUP_MAP_POLL_SECONDS = _resolve_setting(
    "CAO_JSONL_STARTUP_MAP_POLL_SECONDS",
    ("jsonl", "startup_map", "poll_seconds"),
    1.0,
    _parse_float,
)
CAO_JSONL_GATE_DISAGREEMENT_THRESHOLD = _resolve_setting(
    "CAO_JSONL_GATE_DISAGREEMENT_THRESHOLD",
    ("jsonl", "gates", "disagreement_threshold"),
    0.01,
    _parse_float,
)
CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT = _resolve_setting(
    "CAO_JSONL_GATE_ENFORCE_TMUX_DISAGREEMENT",
    ("jsonl", "gates", "enforce_tmux_disagreement"),
    False,
    _parse_bool,
)
CAO_JSONL_GATE_FALLBACK_THRESHOLD = _resolve_setting(
    "CAO_JSONL_GATE_FALLBACK_THRESHOLD",
    ("jsonl", "gates", "fallback_threshold"),
    0.05,
    _parse_float,
)
CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD = _resolve_setting(
    "CAO_JSONL_GATE_WATCHER_ERROR_THRESHOLD",
    ("jsonl", "gates", "watcher_error_threshold"),
    0.001,
    _parse_float,
)
CAO_JSONL_TMUX_COMPARISON_ENABLED = _resolve_setting(
    "CAO_JSONL_TMUX_COMPARISON_ENABLED",
    ("jsonl", "gates", "tmux_comparison_enabled"),
    False,
    _parse_bool,
)

# Rollout scope configuration (for canary gating)
CAO_JSONL_ROLLOUT_TERMINAL_IDS = _resolve_setting(
    "CAO_JSONL_ROLLOUT_TERMINAL_IDS",
    ("jsonl", "rollout", "terminal_ids"),
    set(),
    _parse_set_value,
)
CAO_JSONL_ROLLOUT_SESSION_NAMES = _resolve_setting(
    "CAO_JSONL_ROLLOUT_SESSION_NAMES",
    ("jsonl", "rollout", "session_names"),
    set(),
    _parse_set_value,
)

# Inbox reliability configuration
CAO_INBOX_MAX_DELIVERY_ATTEMPTS = _resolve_setting(
    "CAO_INBOX_MAX_DELIVERY_ATTEMPTS",
    ("inbox", "max_delivery_attempts"),
    5,
    _parse_int,
)
CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS = _resolve_setting(
    "CAO_INBOX_RETRY_BACKOFF_BASE_SECONDS",
    ("inbox", "retry", "base_seconds"),
    2,
    _parse_int,
)
CAO_INBOX_RETRY_BACKOFF_MULTIPLIER = _resolve_setting(
    "CAO_INBOX_RETRY_BACKOFF_MULTIPLIER",
    ("inbox", "retry", "multiplier"),
    2.0,
    _parse_float,
)
CAO_INBOX_RETRY_BACKOFF_MAX_SECONDS = _resolve_setting(
    "CAO_INBOX_RETRY_BACKOFF_MAX_SECONDS",
    ("inbox", "retry", "max_seconds"),
    60,
    _parse_int,
)
CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT = _resolve_setting(
    "CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT",
    ("inbox", "requeue_terminal_state_default"),
    False,
    _parse_bool,
)

# Approval queue configuration
CAO_APPROVAL_QUEUE_ENABLED = _resolve_setting(
    "CAO_APPROVAL_QUEUE_ENABLED",
    ("approvals", "enabled"),
    True,
    _parse_bool,
)
CAO_APPROVAL_PROMPT_TAIL_LINES = _resolve_setting(
    "CAO_APPROVAL_PROMPT_TAIL_LINES",
    ("approvals", "prompt_tail_lines"),
    25,
    _parse_int,
)

# Single-writer safety lock for API server instances sharing the same DB/watchers.
CAO_SINGLE_WRITER_ENFORCED = _resolve_setting(
    "CAO_SINGLE_WRITER_ENFORCED",
    ("single_writer", "enforced"),
    True,
    _parse_bool,
)
CAO_SINGLE_WRITER_ALLOW_OVERRIDE = _resolve_setting(
    "CAO_SINGLE_WRITER_ALLOW_OVERRIDE",
    ("single_writer", "allow_override"),
    False,
    _parse_bool,
)
CAO_SINGLE_WRITER_LOCKFILE = Path(
    _resolve_setting(
        "CAO_SINGLE_WRITER_LOCKFILE",
        ("single_writer", "lockfile"),
        str(DB_DIR / "server-writer.lock"),
        lambda v: str(v),
    )
)
