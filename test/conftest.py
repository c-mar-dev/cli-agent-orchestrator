"""Shared pytest fixtures for CARO-FORK tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.manager import ProviderManager


@dataclass
class FakePane:
    """In-memory pane model for FakeTmuxClient."""

    content: str = ""
    working_directory: Optional[str] = None
    env: Dict[str, str] = field(default_factory=dict)
    content_sequence: Optional[List[str]] = None
    _content_index: int = 0

    def next_content(self) -> str:
        """Return pane content, advancing through content_sequence when present."""
        if self.content_sequence:
            idx = min(self._content_index, len(self.content_sequence) - 1)
            value = self.content_sequence[idx]
            self._content_index += 1
            return value
        return self.content


class FakeTmuxClient:
    """In-memory replacement for TmuxClient used in tests."""

    def __init__(self) -> None:
        self._sessions: Dict[str, Dict[str, FakePane]] = {}
        self._keys_sent: List[Tuple[str, str, str]] = []
        self._raise_on_send_keys: bool = False
        self._piped_panes: List[Tuple[str, str, str]] = []
        self._stopped_pipes: List[Tuple[str, str]] = []

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
    ) -> str:
        if session_name in self._sessions:
            raise ValueError(f"Session '{session_name}' already exists")
        pane = FakePane(
            working_directory=working_directory,
            env={"CAO_TERMINAL_ID": terminal_id},
        )
        self._sessions[session_name] = {window_name: pane}
        return window_name

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
    ) -> str:
        session = self._sessions.get(session_name)
        if session is None:
            raise ValueError(f"Session '{session_name}' not found")
        actual_name = window_name
        if actual_name in session:
            suffix = 1
            while f"{window_name}-{suffix}" in session:
                suffix += 1
            actual_name = f"{window_name}-{suffix}"
        session[actual_name] = FakePane(
            working_directory=working_directory,
            env={"CAO_TERMINAL_ID": terminal_id},
        )
        return actual_name

    def send_keys(self, session_name: str, window_name: str, keys: str) -> None:
        if self._raise_on_send_keys:
            raise RuntimeError("send_keys failure injected by test")
        pane = self._get_pane(session_name, window_name)
        self._keys_sent.append((session_name, window_name, keys))
        pane.content = f"{pane.content}\n{keys}" if pane.content else keys

    def get_history(
        self, session_name: str, window_name: str, tail_lines: Optional[int] = None
    ) -> str:
        pane = self._get_pane(session_name, window_name)
        content = pane.next_content()
        if tail_lines is None:
            return content
        lines = content.splitlines()
        return "\n".join(lines[-tail_lines:])

    def list_sessions(self) -> List[Dict[str, str]]:
        return [
            {"id": name, "name": name, "status": "detached"}
            for name in sorted(self._sessions.keys())
        ]

    def get_session_windows(self, session_name: str) -> List[Dict[str, str]]:
        windows = self._sessions.get(session_name, {})
        return [
            {"name": name, "index": str(i)}
            for i, name in enumerate(windows.keys())
        ]

    def kill_session(self, session_name: str) -> bool:
        return self._sessions.pop(session_name, None) is not None

    def session_exists(self, session_name: str) -> bool:
        return session_name in self._sessions

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        pane = self._sessions.get(session_name, {}).get(window_name)
        if pane is None:
            return None
        return pane.working_directory

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        self._get_pane(session_name, window_name)
        self._piped_panes.append((session_name, window_name, file_path))

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        self._get_pane(session_name, window_name)
        self._stopped_pipes.append((session_name, window_name))

    def set_pane_content(
        self,
        session_name: str,
        window_name: str,
        *,
        content: Optional[str] = None,
        content_sequence: Optional[List[str]] = None,
    ) -> None:
        pane = self._get_pane(session_name, window_name)
        if content is not None:
            pane.content = content
            pane.content_sequence = None
            pane._content_index = 0
        if content_sequence is not None:
            pane.content_sequence = list(content_sequence)
            pane._content_index = 0

    def _get_pane(self, session_name: str, window_name: str) -> FakePane:
        session = self._sessions.get(session_name)
        if session is None:
            raise ValueError(f"Session '{session_name}' not found")
        pane = session.get(window_name)
        if pane is None:
            raise ValueError(f"Window '{window_name}' not found in session '{session_name}'")
        return pane


class FakeProvider(BaseProvider):
    """Provider with deterministic programmable statuses for tests."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        status_sequence: Optional[List[TerminalStatus]] = None,
        last_message: str = "fake provider output",
    ) -> None:
        super().__init__(terminal_id, session_name, window_name)
        self.status_sequence = status_sequence or [TerminalStatus.IDLE]
        self._status_index = 0
        self._last_message = last_message
        self._initialized = False
        self._exited = False

    def initialize(self) -> bool:
        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        idx = min(self._status_index, len(self.status_sequence) - 1)
        status = self.status_sequence[idx]
        if self._status_index < len(self.status_sequence) - 1:
            self._status_index += 1
        self._update_status(status)
        return status

    def get_idle_pattern_for_log(self) -> str:
        return r".*"

    def extract_last_message_from_script(self, script_output: str) -> str:
        return self._last_message

    def exit_cli(self) -> str:
        self._exited = True
        return "/exit"

    def cleanup(self) -> None:
        self._exited = True

    def uses_jsonl_status(self) -> bool:
        return False

    def get_tmux_status(self, tail_lines: Optional[int] = None) -> Optional[TerminalStatus]:
        return self.get_status(tail_lines=tail_lines)


@pytest.fixture
def in_memory_db(monkeypatch: pytest.MonkeyPatch):
    """Patch database module singletons to in-memory SQLite with shared connection."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_session_local = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)

    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(database, "SessionLocal", test_session_local)
    database.Base.metadata.drop_all(bind=test_engine)
    database.Base.metadata.create_all(bind=test_engine)
    database.init_db()

    yield test_engine

    test_engine.dispose()


@pytest.fixture
def fake_tmux(monkeypatch: pytest.MonkeyPatch) -> FakeTmuxClient:
    """Patch tmux singleton in all importing modules with FakeTmuxClient."""
    fake = FakeTmuxClient()
    patch_paths = [
        "cli_agent_orchestrator.clients.tmux.tmux_client",
        "cli_agent_orchestrator.services.terminal_service.tmux_client",
        "cli_agent_orchestrator.services.session_service.tmux_client",
        "cli_agent_orchestrator.providers.codex.tmux_client",
        "cli_agent_orchestrator.providers.claude_code.tmux_client",
        "cli_agent_orchestrator.providers.q_cli.tmux_client",
        "cli_agent_orchestrator.providers.kiro_cli.tmux_client",
        "cli_agent_orchestrator.parsing.jsonl_status_engine.tmux_client",
    ]
    for path in patch_paths:
        monkeypatch.setattr(path, fake)
    return fake


@pytest.fixture
def fake_provider_manager(monkeypatch: pytest.MonkeyPatch) -> ProviderManager:
    """Patch provider_manager singleton across modules with a fresh fake-backed manager."""
    manager = ProviderManager()

    def _create_provider(
        provider_type: str,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str] = None,
    ) -> BaseProvider:
        provider = FakeProvider(terminal_id, tmux_session, tmux_window)
        manager._providers[terminal_id] = provider
        return provider

    monkeypatch.setattr(manager, "create_provider", _create_provider)

    patch_paths = [
        "cli_agent_orchestrator.providers.manager.provider_manager",
        "cli_agent_orchestrator.services.terminal_service.provider_manager",
        "cli_agent_orchestrator.services.session_service.provider_manager",
        "cli_agent_orchestrator.services.inbox_service.provider_manager",
        "cli_agent_orchestrator.api.main.provider_manager",
    ]
    for path in patch_paths:
        monkeypatch.setattr(path, manager)

    return manager


@pytest.fixture
def api_client(in_memory_db: Any, monkeypatch: pytest.MonkeyPatch):
    """FastAPI TestClient with in-memory database patched."""
    monkeypatch.setattr("cli_agent_orchestrator.api.main._acquire_single_writer_lock", lambda: None)
    monkeypatch.setattr("cli_agent_orchestrator.api.main._release_single_writer_lock", lambda: None)
    monkeypatch.setattr("cli_agent_orchestrator.api.main.CAO_JSONL_WATCH_ENABLED", False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def terminal_log_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect TERMINAL_LOG_DIR to a per-test temporary directory."""
    target = tmp_path / "terminal-logs"
    target.mkdir(parents=True, exist_ok=True)

    patch_paths = [
        "cli_agent_orchestrator.constants.TERMINAL_LOG_DIR",
        "cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR",
        "cli_agent_orchestrator.services.inbox_service.TERMINAL_LOG_DIR",
        "cli_agent_orchestrator.api.main.TERMINAL_LOG_DIR",
    ]
    for path in patch_paths:
        monkeypatch.setattr(path, target)

    return target


@pytest.fixture
def disable_jsonl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable JSONL mode in modules that import this constant at import time."""
    patch_paths = [
        "cli_agent_orchestrator.constants.CAO_JSONL_ENABLED",
        "cli_agent_orchestrator.services.terminal_service.CAO_JSONL_ENABLED",
        "cli_agent_orchestrator.providers.codex.CAO_JSONL_ENABLED",
        "cli_agent_orchestrator.providers.claude_code.CAO_JSONL_ENABLED",
        "cli_agent_orchestrator.parsing.jsonl_status_engine.CAO_JSONL_ENABLED",
    ]
    for path in patch_paths:
        monkeypatch.setattr(path, False)


@pytest.fixture
def patch_async_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch asyncio.sleep only within mcp_server.server for faster handoff tests."""

    async def _no_sleep(_seconds: float) -> None:
        return None

    class _AsyncioShim:
        sleep = staticmethod(_no_sleep)

    monkeypatch.setattr("cli_agent_orchestrator.mcp_server.server.asyncio", _AsyncioShim())
