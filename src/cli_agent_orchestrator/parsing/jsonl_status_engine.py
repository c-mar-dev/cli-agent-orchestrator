"""JSONL-backed status engine for Claude Code and Codex providers.

This module intentionally ignores subagent logs for terminal status mapping and
uses parent session logs only.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Deque, Dict, List, Optional

from cli_agent_orchestrator.clients.database import get_terminal_metadata, update_terminal_mapping
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import (
    CAO_JSONL_CLAUDE_ROOT,
    CAO_JSONL_CODEX_ROOT,
    CAO_JSONL_ENABLED,
    CAO_JSONL_MAPPING_GRACE_SECONDS,
    CAO_JSONL_STARTUP_MAP_POLL_SECONDS,
    CAO_JSONL_STARTUP_MAP_TIMEOUT_SECONDS,
    CAO_JSONL_TAIL_LINES,
)
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
ANSI_ESCAPE_RE = re.compile(r"\x1B[@-_][0-?]*[ -/]*[@-~]")
CLAUDE_PROGRESS_STALE_SECONDS = 30
CLAUDE_USER_INFLIGHT_STALE_SECONDS = 60
CODEX_ACTIVITY_STALE_SECONDS = 30


@dataclass
class MappingResolution:
    """Resolved provider log mapping for a terminal."""

    deterministic: bool
    reason_code: str
    session_id: Optional[str] = None
    log_path: Optional[Path] = None
    confidence: str = "none"


@dataclass
class JsonlStatusResult:
    """Provider status computed from structured JSONL."""

    deterministic: bool
    reason_code: str
    status: Optional[TerminalStatus] = None
    session_id: Optional[str] = None
    log_path: Optional[Path] = None
    last_message: Optional[str] = None


@dataclass
class _FileStatusCache:
    mtime_ns: int
    size: int
    status: TerminalStatus
    last_message: Optional[str]
    reason_code: str


@dataclass
class _MappingCacheEntry:
    resolved_at: float
    resolution: MappingResolution


@dataclass
class _CodexMeta:
    path: Path
    session_id: str
    cwd: str
    started_at: datetime


@dataclass
class _FileReadState:
    inode: Optional[int]
    offset: int
    events: Deque[Dict[str, Any]]


@dataclass
class _EngineTelemetry:
    status_calls: int = 0
    mapping_attempts: int = 0
    deterministic_mappings: int = 0
    nondeterministic_mappings: int = 0
    mapping_reason_counts: Dict[str, int] = field(default_factory=dict)
    lines_seen: int = 0
    malformed_lines: int = 0
    parser_resets: int = 0
    parse_read_errors: int = 0
    mapping_promotions: int = 0
    mapping_demotions: int = 0
    mapping_invalidations: int = 0
    mapping_invalidation_reason_counts: Dict[str, int] = field(default_factory=dict)
    startup_mapping_attempts: int = 0
    startup_mapping_deterministic: int = 0
    startup_mapping_nondeterministic: int = 0


def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def _tail_json_objects(path: Path, max_lines: int) -> List[Dict[str, Any]]:
    """Read and parse up to max_lines JSON lines from the end of a file."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = deque(f, maxlen=max_lines)
    except OSError:
        return []

    out: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _normalize_match_text(value: str) -> str:
    text = value.replace("\u00a0", " ")
    text = " ".join(text.split())
    return text.strip().lower()


def _extract_claude_user_content(message: Dict[str, Any]) -> Optional[str]:
    content = message.get("content")
    if isinstance(content, str):
        normalized = _normalize_match_text(content)
        return normalized or None
    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text_value = item.get("text")
            if isinstance(text_value, str):
                chunk = _normalize_match_text(text_value)
                if chunk:
                    chunks.append(chunk)
        if chunks:
            return "\n".join(chunks)
    return None


class JsonlStatusEngine:
    """Singleton-style JSONL status engine."""

    def __init__(self) -> None:
        self._mapping_cache: Dict[str, _MappingCacheEntry] = {}
        self._file_cache: Dict[Path, _FileStatusCache] = {}
        self._file_read_state: Dict[Path, _FileReadState] = {}
        self._codex_index: List[_CodexMeta] = []
        self._codex_index_built_at: float = 0.0
        self._warned_no_deterministic_mapping: set[str] = set()
        self._telemetry = _EngineTelemetry()
        self._lock = Lock()

    def get_status(
        self,
        provider_type: str,
        terminal_id: str,
        session_name: str,
        window_name: str,
        *,
        force_refresh_mapping: bool = False,
    ) -> JsonlStatusResult:
        """Get status using structured JSONL logs.

        Returns non-deterministic result when mapping cannot be safely resolved.
        """
        if not CAO_JSONL_ENABLED:
            return JsonlStatusResult(deterministic=False, reason_code="jsonl_disabled")

        if provider_type not in {ProviderType.CLAUDE_CODE.value, ProviderType.CODEX.value}:
            return JsonlStatusResult(deterministic=False, reason_code="provider_not_supported")

        with self._lock:
            self._telemetry.status_calls += 1
            if force_refresh_mapping:
                self._mapping_cache.pop(terminal_id, None)

        mapping = self._resolve_mapping(provider_type, terminal_id, session_name, window_name)
        self._record_mapping_resolution(mapping)

        if not mapping.deterministic or mapping.log_path is None:
            self._mark_mapping_status(terminal_id, mapping)
            self._warn_on_non_deterministic(terminal_id, mapping.reason_code)
            return JsonlStatusResult(
                deterministic=False,
                reason_code=mapping.reason_code,
                session_id=mapping.session_id,
                log_path=mapping.log_path,
            )

        parse_result = self._parse_provider_log(provider_type, mapping.log_path)
        self._mark_mapping_status(terminal_id, mapping, parse_result.reason_code)
        return JsonlStatusResult(
            deterministic=True,
            reason_code=parse_result.reason_code,
            status=parse_result.status,
            session_id=mapping.session_id,
            log_path=mapping.log_path,
            last_message=parse_result.last_message,
        )

    def capture_startup_mapping(
        self,
        provider_type: str,
        terminal_id: str,
        session_name: str,
        window_name: str,
        *,
        timeout_seconds: Optional[float] = None,
        poll_seconds: Optional[float] = None,
    ) -> MappingResolution:
        """Attempt deterministic mapping capture immediately after terminal startup."""
        timeout_seconds = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else float(CAO_JSONL_STARTUP_MAP_TIMEOUT_SECONDS)
        )
        poll_seconds = (
            float(poll_seconds)
            if poll_seconds is not None
            else float(CAO_JSONL_STARTUP_MAP_POLL_SECONDS)
        )
        deadline = time.time() + max(timeout_seconds, 0.0)
        with self._lock:
            self._telemetry.startup_mapping_attempts += 1

        last = MappingResolution(
            deterministic=False,
            reason_code="startup_mapping_timeout",
            confidence="none",
        )

        while time.time() <= deadline:
            with self._lock:
                self._mapping_cache.pop(terminal_id, None)
            mapping = self._resolve_mapping(provider_type, terminal_id, session_name, window_name)
            self._record_mapping_resolution(mapping)
            self._mark_mapping_status(terminal_id, mapping)
            if mapping.deterministic:
                with self._lock:
                    self._telemetry.startup_mapping_deterministic += 1
                return mapping
            last = mapping
            if poll_seconds <= 0:
                break
            time.sleep(poll_seconds)

        self._warn_on_non_deterministic(terminal_id, last.reason_code)
        with self._lock:
            self._telemetry.startup_mapping_nondeterministic += 1
        return last

    def get_telemetry_snapshot(self) -> Dict[str, Any]:
        """Return telemetry counters for migration diagnostics."""
        with self._lock:
            reason_counts = dict(self._telemetry.mapping_reason_counts)
            return {
                "status_calls": self._telemetry.status_calls,
                "mapping_attempts": self._telemetry.mapping_attempts,
                "deterministic_mappings": self._telemetry.deterministic_mappings,
                "nondeterministic_mappings": self._telemetry.nondeterministic_mappings,
                "mapping_reason_counts": reason_counts,
                "lines_seen": self._telemetry.lines_seen,
                "malformed_lines": self._telemetry.malformed_lines,
                "parser_resets": self._telemetry.parser_resets,
                "parse_read_errors": self._telemetry.parse_read_errors,
                "mapping_promotions": self._telemetry.mapping_promotions,
                "mapping_demotions": self._telemetry.mapping_demotions,
                "mapping_invalidations": self._telemetry.mapping_invalidations,
                "mapping_invalidation_reason_counts": dict(
                    self._telemetry.mapping_invalidation_reason_counts
                ),
                "startup_mapping_attempts": self._telemetry.startup_mapping_attempts,
                "startup_mapping_deterministic": self._telemetry.startup_mapping_deterministic,
                "startup_mapping_nondeterministic": self._telemetry.startup_mapping_nondeterministic,
            }

    def reset_telemetry(self) -> None:
        """Reset telemetry counters (used by tests)."""
        with self._lock:
            self._telemetry = _EngineTelemetry()

    def _record_mapping_resolution(self, mapping: MappingResolution) -> None:
        with self._lock:
            self._telemetry.mapping_attempts += 1
            if mapping.deterministic:
                self._telemetry.deterministic_mappings += 1
            else:
                self._telemetry.nondeterministic_mappings += 1
            self._telemetry.mapping_reason_counts[mapping.reason_code] = (
                self._telemetry.mapping_reason_counts.get(mapping.reason_code, 0) + 1
            )

    def _mark_mapping_status(
        self,
        terminal_id: str,
        mapping: MappingResolution,
        status_reason_code: Optional[str] = None,
    ) -> None:
        status_source = "jsonl" if mapping.deterministic else "hybrid"
        try:
            previous = get_terminal_metadata(terminal_id) or {}
            previous_source = str(previous.get("status_source") or "").strip().lower()
            update_terminal_mapping(
                terminal_id,
                provider_session_id=mapping.session_id,
                provider_log_path=str(mapping.log_path) if mapping.log_path else None,
                status_source=status_source,
                mapping_confidence=mapping.confidence,
                status_reason_code=status_reason_code or mapping.reason_code,
            )
            with self._lock:
                if previous_source != "jsonl" and status_source == "jsonl":
                    self._telemetry.mapping_promotions += 1
                elif previous_source == "jsonl" and status_source != "jsonl":
                    self._telemetry.mapping_demotions += 1
        except Exception as exc:
            logger.debug("Skipping mapping metadata update for %s: %s", terminal_id, exc)

    def _record_mapping_invalidation(self, reason_code: str) -> None:
        with self._lock:
            self._telemetry.mapping_invalidations += 1
            self._telemetry.mapping_invalidation_reason_counts[reason_code] = (
                self._telemetry.mapping_invalidation_reason_counts.get(reason_code, 0) + 1
            )

    def _warn_on_non_deterministic(self, terminal_id: str, reason_code: str) -> None:
        if terminal_id in self._warned_no_deterministic_mapping:
            return
        self._warned_no_deterministic_mapping.add(terminal_id)
        logger.warning(
            "JSONL deterministic session mapping unavailable for terminal=%s reason=%s; "
            "falling back to tmux status detection",
            terminal_id,
            reason_code,
        )

    def _resolve_mapping(
        self,
        provider_type: str,
        terminal_id: str,
        session_name: str,
        window_name: str,
    ) -> MappingResolution:
        now = time.time()
        cached = self._mapping_cache.get(terminal_id)
        if cached and now - cached.resolved_at < 5:
            return cached.resolution

        try:
            metadata = get_terminal_metadata(terminal_id) or {}
        except Exception as exc:
            logger.debug("Failed to read terminal metadata for %s: %s", terminal_id, exc)
            metadata = {}
        launch_cwd = metadata.get("launch_cwd") or tmux_client.get_pane_working_directory(
            session_name, window_name
        )
        created_at = metadata.get("created_at")
        created_at_utc: Optional[datetime]
        if isinstance(created_at, datetime):
            created_at_utc = (
                created_at.replace(tzinfo=timezone.utc)
                if created_at.tzinfo is None
                else created_at.astimezone(timezone.utc)
            )
        else:
            created_at_utc = None

        # Honor previously persisted deterministic mapping unless hard evidence invalidates it.
        mapped_session = metadata.get("provider_session_id")
        mapped_session_id = str(mapped_session) if isinstance(mapped_session, str) else None
        mapped_log_path = metadata.get("provider_log_path")
        if mapped_session_id and mapped_log_path:
            mapped_path = Path(mapped_log_path)
            if not mapped_path.exists():
                self._record_mapping_invalidation("persisted_log_path_missing")
                logger.info(
                    "Invalidating persisted mapping terminal=%s reason=persisted_log_path_missing path=%s",
                    terminal_id,
                    mapped_path,
                )
            else:
                resolution = MappingResolution(
                    deterministic=True,
                    reason_code="persisted_mapping",
                    session_id=mapped_session_id,
                    log_path=mapped_path,
                    confidence="high",
                )
                self._mapping_cache[terminal_id] = _MappingCacheEntry(now, resolution)
                return resolution

        if not launch_cwd:
            resolution = MappingResolution(
                deterministic=False,
                reason_code="missing_launch_cwd",
                confidence="none",
            )
            self._mapping_cache[terminal_id] = _MappingCacheEntry(now, resolution)
            return resolution

        if provider_type == ProviderType.CLAUDE_CODE.value:
            resolution = self._resolve_claude_mapping(
                launch_cwd,
                created_at_utc,
                session_name,
                window_name,
                preferred_session_id=mapped_session_id,
            )
        else:
            resolution = self._resolve_codex_mapping(
                launch_cwd,
                created_at_utc,
                preferred_session_id=mapped_session_id,
            )

        self._mapping_cache[terminal_id] = _MappingCacheEntry(now, resolution)
        return resolution

    def _resolve_claude_mapping(
        self,
        launch_cwd: str,
        created_at: Optional[datetime],
        session_name: str,
        window_name: str,
        preferred_session_id: Optional[str] = None,
    ) -> MappingResolution:
        project_bucket = launch_cwd.replace("/", "-")
        project_dir = CAO_JSONL_CLAUDE_ROOT / project_bucket
        if not project_dir.exists():
            return MappingResolution(
                deterministic=False,
                reason_code="claude_project_bucket_missing",
                confidence="none",
            )

        if preferred_session_id and UUID_RE.match(preferred_session_id):
            hinted_path = project_dir / f"{preferred_session_id}.jsonl"
            if hinted_path.exists():
                return MappingResolution(
                    deterministic=True,
                    reason_code="claude_session_hint_match",
                    session_id=preferred_session_id,
                    log_path=hinted_path,
                    confidence="high",
                )

        candidates: List[Path] = []
        for path in project_dir.glob("*.jsonl"):
            # Parent sessions only. Ignore non-UUID files (e.g. agent-* subagent files).
            if not UUID_RE.match(path.stem):
                continue
            candidates.append(path)

        if not candidates:
            return MappingResolution(
                deterministic=False,
                reason_code="claude_no_parent_session_logs",
                confidence="none",
            )

        recent: List[Path] = []
        if created_at is not None:
            started_after_create: List[Path] = []
            start_window = created_at + timedelta(seconds=CAO_JSONL_MAPPING_GRACE_SECONDS)
            for path in candidates:
                started_at = self._extract_claude_session_started_at(path)
                if started_at is None:
                    continue
                if created_at <= started_at <= start_window:
                    started_after_create.append(path)
            if len(started_after_create) == 1:
                chosen = started_after_create[0]
                return MappingResolution(
                    deterministic=True,
                    reason_code="claude_single_started_after_create",
                    session_id=chosen.stem,
                    log_path=chosen,
                    confidence="high",
                )
            if len(started_after_create) > 1:
                matched = self._resolve_claude_mapping_from_tmux_user_text(
                    started_after_create, session_name, window_name
                )
                if matched is not None:
                    return matched

            cutoff = created_at - timedelta(seconds=CAO_JSONL_MAPPING_GRACE_SECONDS)
            for path in candidates:
                try:
                    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                except OSError:
                    continue
                if mtime >= cutoff:
                    recent.append(path)
            if len(recent) == 1:
                chosen = recent[0]
                return MappingResolution(
                    deterministic=True,
                    reason_code="claude_single_recent_parent_session",
                    session_id=chosen.stem,
                    log_path=chosen,
                    confidence="high",
                )
            if len(recent) > 1:
                matched = self._resolve_claude_mapping_from_tmux_user_text(
                    recent, session_name, window_name
                )
                if matched is not None:
                    return matched

        if len(candidates) == 1:
            chosen = candidates[0]
            return MappingResolution(
                deterministic=True,
                reason_code="claude_single_parent_session",
                session_id=chosen.stem,
                log_path=chosen,
                confidence="high",
            )

        matched = self._resolve_claude_mapping_from_tmux_user_text(
            candidates, session_name, window_name
        )
        if matched is not None:
            return matched

        return MappingResolution(
            deterministic=False,
            reason_code="claude_ambiguous_parent_sessions",
            confidence="none",
        )

    def _resolve_claude_mapping_from_tmux_user_text(
        self, candidates: List[Path], session_name: str, window_name: str
    ) -> Optional[MappingResolution]:
        snippets = self._extract_claude_tmux_user_snippets(session_name, window_name)
        if not snippets:
            return None

        scored: List[tuple[int, int, Path]] = []
        for path in candidates:
            user_messages = self._extract_claude_log_user_messages(path)
            if not user_messages:
                continue
            matched_count = 0
            matched_length = 0
            for snippet in snippets:
                if any(snippet in msg or msg in snippet for msg in user_messages):
                    matched_count += 1
                    matched_length += len(snippet)
            if matched_count > 0:
                scored.append((matched_count, matched_length, path))

        if not scored:
            return None

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        best = scored[0]
        if len(scored) > 1:
            second = scored[1]
            if second[0] == best[0] and second[1] == best[1]:
                return None

        chosen = best[2]
        return MappingResolution(
            deterministic=True,
            reason_code="claude_tmux_user_match",
            session_id=chosen.stem,
            log_path=chosen,
            confidence="medium",
        )

    def _extract_claude_tmux_user_snippets(self, session_name: str, window_name: str) -> List[str]:
        try:
            scrollback = tmux_client.get_history(session_name, window_name, tail_lines=400)
        except Exception:
            return []

        cleaned = ANSI_ESCAPE_RE.sub("", scrollback).replace("\r", "")
        out: List[str] = []
        seen: set[str] = set()
        for raw_line in cleaned.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            if line.startswith(("❯", "›", ">")):
                candidate = line[1:].strip()
            else:
                continue

            lowered = candidate.lower()
            if re.match(r"^\d+\.", candidate):
                continue
            if lowered.startswith("enter to select"):
                continue
            if lowered.startswith("to navigate"):
                continue
            if lowered.startswith("esc to cancel"):
                continue

            normalized = _normalize_match_text(candidate)
            if len(normalized) < 8:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            out.append(normalized)

        return out[-8:]

    def _extract_claude_log_user_messages(self, log_path: Path) -> List[str]:
        events = _tail_json_objects(log_path, CAO_JSONL_TAIL_LINES)
        out: List[str] = []
        seen: set[str] = set()
        for obj in events:
            if obj.get("type") != "user":
                continue
            message = obj.get("message")
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            extracted = _extract_claude_user_content(message)
            if not extracted or extracted in seen:
                continue
            out.append(extracted)
            seen.add(extracted)
        return out

    def _claude_mapping_matches_tmux_user_text(
        self, log_path: Path, session_name: str, window_name: str
    ) -> bool:
        snippets = self._extract_claude_tmux_user_snippets(session_name, window_name)
        if not snippets:
            return True
        user_messages = self._extract_claude_log_user_messages(log_path)
        if not user_messages:
            return True
        return any(any(snippet in msg or msg in snippet for msg in user_messages) for snippet in snippets)

    def _extract_claude_session_started_at(self, log_path: Path) -> Optional[datetime]:
        try:
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                for _ in range(25):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    parsed = _parse_timestamp(obj.get("timestamp"))
                    if parsed is not None:
                        return parsed
                    snapshot = obj.get("snapshot")
                    if isinstance(snapshot, dict):
                        parsed = _parse_timestamp(snapshot.get("timestamp"))
                        if parsed is not None:
                            return parsed
        except OSError:
            return None
        return None

    def _resolve_codex_mapping(
        self,
        launch_cwd: str,
        created_at: Optional[datetime],
        preferred_session_id: Optional[str] = None,
    ) -> MappingResolution:
        index = self._get_codex_index()
        if not index:
            return MappingResolution(
                deterministic=False,
                reason_code="codex_no_session_meta_files",
                confidence="none",
            )

        normalized_launch_cwd = self._normalize_path(launch_cwd)
        if preferred_session_id and UUID_RE.match(preferred_session_id):
            hinted_candidates = [entry for entry in index if entry.session_id == preferred_session_id]
            if len(hinted_candidates) == 1:
                chosen = hinted_candidates[0]
                return MappingResolution(
                    deterministic=True,
                    reason_code="codex_session_hint_match",
                    session_id=chosen.session_id,
                    log_path=chosen.path,
                    confidence="high",
                )

        candidates = [
            entry for entry in index if self._normalize_path(entry.cwd) == normalized_launch_cwd
        ]
        if created_at is not None:
            cutoff = created_at - timedelta(seconds=CAO_JSONL_MAPPING_GRACE_SECONDS)
            candidates = [entry for entry in candidates if entry.started_at >= cutoff]

        if len(candidates) == 1:
            chosen = candidates[0]
            return MappingResolution(
                deterministic=True,
                reason_code="codex_single_matching_session",
                session_id=chosen.session_id,
                log_path=chosen.path,
                confidence="high",
            )

        if not candidates:
            return MappingResolution(
                deterministic=False,
                reason_code="codex_no_matching_session",
                confidence="none",
            )

        return MappingResolution(
            deterministic=False,
            reason_code="codex_ambiguous_sessions",
            confidence="none",
        )

    def _get_codex_index(self) -> List[_CodexMeta]:
        now = time.time()
        if self._codex_index and now - self._codex_index_built_at < 15:
            return self._codex_index

        out: List[_CodexMeta] = []
        if not CAO_JSONL_CODEX_ROOT.exists():
            self._codex_index = out
            self._codex_index_built_at = now
            return out

        for path in CAO_JSONL_CODEX_ROOT.rglob("rollout-*.jsonl"):
            try:
                with path.open("r", encoding="utf-8", errors="replace") as f:
                    obj: Optional[Dict[str, Any]] = None
                    for _ in range(25):
                        candidate = f.readline()
                        if not candidate:
                            break
                        candidate = candidate.strip()
                        if not candidate:
                            continue
                        try:
                            parsed = json.loads(candidate)
                        except json.JSONDecodeError:
                            continue
                        if not isinstance(parsed, dict):
                            continue
                        if parsed.get("type") == "session_meta":
                            obj = parsed
                            break
            except OSError:
                continue
            if not isinstance(obj, dict):
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            session_id = payload.get("id")
            cwd = payload.get("cwd")
            started_at = _parse_timestamp(payload.get("timestamp"))
            if (
                not isinstance(session_id, str)
                or not isinstance(cwd, str)
                or started_at is None
                or not UUID_RE.match(session_id)
            ):
                continue
            if session_id not in path.stem:
                continue
            out.append(_CodexMeta(path=path, session_id=session_id, cwd=cwd, started_at=started_at))

        self._codex_index = out
        self._codex_index_built_at = now
        return out

    @staticmethod
    def _normalize_path(value: str) -> str:
        try:
            return str(Path(value).expanduser().resolve())
        except Exception:
            return str(value)

    def _parse_provider_log(self, provider_type: str, log_path: Path) -> _FileStatusCache:
        try:
            stat = log_path.stat()
        except OSError:
            with self._lock:
                self._telemetry.parse_read_errors += 1
            return _FileStatusCache(
                mtime_ns=0,
                size=0,
                status=TerminalStatus.ERROR,
                last_message=None,
                reason_code="log_stat_failed",
            )

        cached = self._file_cache.get(log_path)
        if cached and cached.mtime_ns == stat.st_mtime_ns and cached.size == stat.st_size:
            return cached

        force_reopen = bool(
            cached and cached.size == stat.st_size and cached.mtime_ns != stat.st_mtime_ns
        )
        events = self._read_events_incremental(log_path, stat, force_reopen=force_reopen)
        if provider_type == ProviderType.CLAUDE_CODE.value:
            status, last_message, reason = self._parse_claude_events(events)
        else:
            status, last_message, reason = self._parse_codex_events(events)

        parsed = _FileStatusCache(
            mtime_ns=stat.st_mtime_ns,
            size=stat.st_size,
            status=status,
            last_message=last_message,
            reason_code=reason,
        )
        self._file_cache[log_path] = parsed
        return parsed

    def _read_events_incremental(
        self, path: Path, stat: Any, *, force_reopen: bool = False
    ) -> List[Dict[str, Any]]:
        inode = getattr(stat, "st_ino", None)
        state = self._file_read_state.get(path)

        if state is None:
            events = deque(
                _tail_json_objects(path, CAO_JSONL_TAIL_LINES), maxlen=CAO_JSONL_TAIL_LINES
            )
            self._file_read_state[path] = _FileReadState(
                inode=inode,
                offset=int(stat.st_size),
                events=events,
            )
            return list(events)

        rotated = state.inode is not None and inode is not None and state.inode != inode
        truncated = int(stat.st_size) < state.offset
        if rotated or truncated or force_reopen:
            with self._lock:
                self._telemetry.parser_resets += 1
            events = deque(
                _tail_json_objects(path, CAO_JSONL_TAIL_LINES), maxlen=CAO_JSONL_TAIL_LINES
            )
            self._file_read_state[path] = _FileReadState(
                inode=inode,
                offset=int(stat.st_size),
                events=events,
            )
            return list(events)

        if int(stat.st_size) == state.offset:
            return list(state.events)

        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(state.offset)
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    with self._lock:
                        self._telemetry.lines_seen += 1
                    try:
                        obj = json.loads(stripped)
                    except json.JSONDecodeError:
                        with self._lock:
                            self._telemetry.malformed_lines += 1
                        continue
                    if isinstance(obj, dict):
                        state.events.append(obj)
                state.offset = f.tell()
                state.inode = inode
        except OSError:
            with self._lock:
                self._telemetry.parse_read_errors += 1
            return list(state.events)

        return list(state.events)

    def _parse_claude_events(
        self, events: List[Dict[str, Any]]
    ) -> tuple[TerminalStatus, Optional[str], str]:
        now = datetime.now(timezone.utc)
        last_user: Optional[datetime] = None
        last_user_tool_result: Optional[datetime] = None
        last_assistant: Optional[datetime] = None
        last_processing: Optional[datetime] = None
        last_hook_stop: Optional[datetime] = None
        last_waiting: Optional[datetime] = None
        waiting_reason_code: Optional[str] = None
        last_error: Optional[datetime] = None
        last_message: Optional[str] = None

        def _apply_claude_message_event(
            *,
            event_ts: Optional[datetime],
            role: Optional[str],
            message_obj: Any,
        ) -> None:
            nonlocal last_user
            nonlocal last_user_tool_result
            nonlocal last_assistant
            nonlocal last_waiting
            nonlocal waiting_reason_code
            nonlocal last_message

            if not isinstance(message_obj, dict):
                return

            message_role = message_obj.get("role")
            effective_role = role or message_role

            if effective_role == "user":
                content = message_obj.get("content")
                if event_ts:
                    last_user = event_ts
                if isinstance(content, list):
                    if any(
                        isinstance(item, dict) and item.get("type") == "tool_result"
                        for item in content
                    ) and event_ts:
                        last_user_tool_result = event_ts
                return

            if effective_role != "assistant":
                return

            if event_ts:
                last_assistant = event_ts

            content = message_obj.get("content")
            if not isinstance(content, list):
                return

            if any(
                isinstance(item, dict)
                and item.get("type") == "tool_use"
                and str(item.get("name", "")).lower() in {"askuserquestion"}
                for item in content
            ) and event_ts:
                last_waiting = event_ts
                waiting_reason_code = "claude_ask_user_question"

            text_chunks = [
                str(item.get("text", "")).strip()
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            combined = "\n".join(chunk for chunk in text_chunks if chunk).strip()
            if combined:
                last_message = combined

        for obj in events:
            ts = _parse_timestamp(obj.get("timestamp"))
            obj_type = obj.get("type")

            if obj_type == "user":
                _apply_claude_message_event(
                    event_ts=ts,
                    role="user",
                    message_obj=obj.get("message"),
                )
                continue

            if obj_type == "assistant":
                _apply_claude_message_event(
                    event_ts=ts,
                    role="assistant",
                    message_obj=obj.get("message"),
                )
                continue

            if obj_type == "progress":
                data = obj.get("data")
                data_type = data.get("type") if isinstance(data, dict) else None
                if not ts:
                    continue
                if data_type == "waiting_for_task":
                    last_waiting = ts
                    waiting_reason_code = "claude_waiting_for_task"
                elif data_type == "hook_progress":
                    hook_event = str(data.get("hookEvent", "")).lower() if isinstance(data, dict) else ""
                    if hook_event == "start":
                        last_processing = ts
                    elif hook_event == "stop":
                        last_hook_stop = ts
                elif data_type in {
                    "bash_progress",
                    "agent_progress",
                    "mcp_progress",
                    "query_update",
                    "search_results_received",
                }:
                    last_processing = ts
                    if data_type == "agent_progress" and isinstance(data, dict):
                        progress_message = data.get("message")
                        if isinstance(progress_message, dict):
                            progress_ts = _parse_timestamp(progress_message.get("timestamp")) or ts
                            progress_role = progress_message.get("type")
                            progress_inner = progress_message.get("message")
                            _apply_claude_message_event(
                                event_ts=progress_ts,
                                role=progress_role if isinstance(progress_role, str) else None,
                                message_obj=progress_inner,
                            )
                continue

            if obj_type == "system":
                if obj.get("subtype") == "api_error" and ts:
                    last_error = ts
                continue

            if obj.get("isApiErrorMessage") is True and ts:
                last_error = ts

        if last_error and (last_user is None or last_error >= last_user):
            if last_assistant is None or last_error >= last_assistant:
                return TerminalStatus.ERROR, last_message, "claude_api_error"

        if last_waiting and (last_processing is None or last_waiting >= last_processing):
            if last_user_tool_result is None or last_waiting >= last_user_tool_result:
                return (
                    TerminalStatus.WAITING_USER_ANSWER,
                    last_message,
                    waiting_reason_code or "claude_waiting_for_task",
                )

        # Stop-hook progress is a strong completion signal and prevents stale
        # processing states when assistant messages are represented via
        # agent_progress entries.
        if last_hook_stop and (last_processing is None or last_hook_stop >= last_processing):
            if last_user is None or last_hook_stop >= last_user:
                return TerminalStatus.COMPLETED, last_message, "claude_hook_stop_completed"

        if last_processing and (last_assistant is None or last_processing > last_assistant):
            if now - last_processing <= timedelta(seconds=CLAUDE_PROGRESS_STALE_SECONDS):
                return TerminalStatus.PROCESSING, last_message, "claude_progress_active"

        # User has submitted a newer turn than the latest assistant response,
        # but progress markers may lag briefly in JSONL. Treat as in-flight work.
        if last_user and (last_assistant is None or last_user > last_assistant):
            if now - last_user <= timedelta(seconds=CLAUDE_USER_INFLIGHT_STALE_SECONDS):
                return TerminalStatus.PROCESSING, last_message, "claude_user_turn_inflight"

        if last_assistant and (last_user is None or last_assistant >= last_user):
            return TerminalStatus.COMPLETED, last_message, "claude_assistant_completed"

        return TerminalStatus.IDLE, last_message, "claude_idle_quiet"

    def _parse_codex_events(
        self, events: List[Dict[str, Any]]
    ) -> tuple[TerminalStatus, Optional[str], str]:
        now = datetime.now(timezone.utc)
        last_user: Optional[datetime] = None
        last_processing: Optional[datetime] = None
        last_task_started: Optional[datetime] = None
        last_task_complete: Optional[datetime] = None
        last_error: Optional[datetime] = None
        last_message: Optional[str] = None
        waiting_unsupported = False

        for obj in events:
            ts = _parse_timestamp(obj.get("timestamp"))
            obj_type = obj.get("type")

            if obj_type == "event_msg":
                payload = obj.get("payload")
                payload_type = payload.get("type") if isinstance(payload, dict) else None
                if not ts:
                    continue
                if payload_type == "user_message":
                    last_user = ts
                elif payload_type == "task_started":
                    last_task_started = ts
                    last_processing = ts
                elif payload_type == "task_complete":
                    last_task_complete = ts
                    msg = payload.get("last_agent_message") if isinstance(payload, dict) else None
                    if isinstance(msg, str) and msg.strip():
                        last_message = msg.strip()
                elif payload_type in {"token_count", "agent_reasoning", "agent_message"}:
                    last_processing = ts
                    if (
                        payload_type == "agent_message"
                        and isinstance(payload, dict)
                        and isinstance(payload.get("message"), str)
                        and payload.get("message", "").strip()
                    ):
                        last_message = payload.get("message", "").strip()
                elif payload_type == "turn_aborted":
                    reason = payload.get("reason") if isinstance(payload, dict) else None
                    if reason and reason != "interrupted":
                        last_error = ts
                continue

            if obj_type != "response_item":
                continue

            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            payload_type = payload.get("type")

            if payload_type == "message" and payload.get("role") == "assistant":
                content = payload.get("content")
                if isinstance(content, list):
                    text_chunks = [
                        str(item.get("text", "")).strip()
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "output_text"
                    ]
                    combined = "\n".join(chunk for chunk in text_chunks if chunk).strip()
                    if combined:
                        last_message = combined
                if ts:
                    last_processing = ts
                continue

            if payload_type in {"reasoning", "custom_tool_call", "custom_tool_call_output"}:
                if ts:
                    last_processing = ts
                continue

            if payload_type == "function_call":
                if ts:
                    last_processing = ts
                args = str(payload.get("arguments", ""))
                name = str(payload.get("name", ""))
                if "require_escalated" in args or name == "request_user_input":
                    waiting_unsupported = True
                continue

            if payload_type == "function_call_output":
                output = str(payload.get("output", ""))
                lowered = output.lower()
                if ts and (
                    "reject command" in lowered
                    or "rejected by user" in lowered
                    or lowered.startswith("error:")
                ):
                    last_error = ts
                if ts:
                    last_processing = ts

        if last_error and (last_task_complete is None or last_error >= last_task_complete):
            return TerminalStatus.ERROR, last_message, "codex_error_event"

        if last_task_complete and (
            last_task_started is None or last_task_complete >= last_task_started
        ):
            return TerminalStatus.COMPLETED, last_message, "codex_task_complete"

        recent_task_started = bool(
            last_task_started
            and now - last_task_started <= timedelta(seconds=CODEX_ACTIVITY_STALE_SECONDS)
        )
        recent_processing = bool(
            last_processing and now - last_processing <= timedelta(seconds=CODEX_ACTIVITY_STALE_SECONDS)
        )
        if recent_task_started or recent_processing or waiting_unsupported:
            reason = (
                "codex_waiting_unsupported" if waiting_unsupported else "codex_processing_active"
            )
            return TerminalStatus.PROCESSING, last_message, reason

        if last_user and last_message:
            return TerminalStatus.COMPLETED, last_message, "codex_assistant_completed"

        return TerminalStatus.IDLE, last_message, "codex_idle_quiet"


jsonl_status_engine = JsonlStatusEngine()
