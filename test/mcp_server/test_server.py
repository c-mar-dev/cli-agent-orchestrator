"""Tests for MCP server orchestration helpers and tools."""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.mcp_server import server


class _Response:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class TestCreateTerminal:
    def test_create_terminal_without_existing_session(self, monkeypatch):
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        monkeypatch.setattr(server, "generate_session_name", lambda: "cao-generated")

        captured = {}

        def _post(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _Response({"id": "worker001"})

        monkeypatch.setattr(server.requests, "post", _post)

        terminal_id, provider = server._create_terminal("developer")

        assert terminal_id == "worker001"
        assert provider == server.DEFAULT_PROVIDER
        assert captured["url"].endswith("/sessions")
        assert captured["params"]["agent_profile"] == "developer"
        assert captured["params"]["session_name"] == "cao-generated"

    def test_create_terminal_with_existing_session(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abc123ef")

        def _get(url, params=None, timeout=None):
            if url.endswith("/terminals/abc123ef"):
                return _Response({"provider": "codex", "session_name": "cao-main"})
            raise AssertionError(f"Unexpected GET URL: {url}")

        captured = {}

        def _post(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _Response({"id": "worker002"})

        monkeypatch.setattr(server.requests, "get", _get)
        monkeypatch.setattr(server.requests, "post", _post)

        terminal_id, provider = server._create_terminal("analyst")

        assert terminal_id == "worker002"
        assert provider == "codex"
        assert captured["url"].endswith("/sessions/cao-main/terminals")
        assert captured["params"]["provider"] == "codex"
        assert captured["params"]["agent_profile"] == "analyst"

    def test_create_terminal_inherits_conductor_working_directory(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abc123ef")

        def _get(url, params=None, timeout=None):
            if url.endswith("/terminals/abc123ef"):
                return _Response({"provider": "q_cli", "session_name": "cao-main"})
            if url.endswith("/terminals/abc123ef/working-directory"):
                return _Response({"working_directory": "/repo/subdir"}, status_code=200)
            raise AssertionError(f"Unexpected GET URL: {url}")

        captured = {}

        def _post(url, params=None, timeout=None):
            captured["params"] = params
            return _Response({"id": "worker003"})

        monkeypatch.setattr(server.requests, "get", _get)
        monkeypatch.setattr(server.requests, "post", _post)

        server._create_terminal("developer")

        assert captured["params"]["working_directory"] == "/repo/subdir"

    def test_create_terminal_explicit_working_directory_overrides_inheritance(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abc123ef")

        get_calls = []

        def _get(url, params=None, timeout=None):
            get_calls.append(url)
            if url.endswith("/terminals/abc123ef"):
                return _Response({"provider": "q_cli", "session_name": "cao-main"})
            raise AssertionError(f"Unexpected GET URL: {url}")

        captured = {}

        def _post(url, params=None, timeout=None):
            captured["params"] = params
            return _Response({"id": "worker004"})

        monkeypatch.setattr(server.requests, "get", _get)
        monkeypatch.setattr(server.requests, "post", _post)

        server._create_terminal("developer", working_directory="/explicit/path")

        assert all(not url.endswith("/working-directory") for url in get_calls)
        assert captured["params"]["working_directory"] == "/explicit/path"

    def test_create_terminal_working_directory_endpoint_500_falls_back(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "abc123ef")

        def _get(url, params=None, timeout=None):
            if url.endswith("/terminals/abc123ef"):
                return _Response({"provider": "q_cli", "session_name": "cao-main"})
            if url.endswith("/terminals/abc123ef/working-directory"):
                return _Response({"detail": "error"}, status_code=500)
            raise AssertionError(f"Unexpected GET URL: {url}")

        captured = {}

        def _post(url, params=None, timeout=None):
            captured["params"] = params
            return _Response({"id": "worker005"})

        monkeypatch.setattr(server.requests, "get", _get)
        monkeypatch.setattr(server.requests, "post", _post)

        server._create_terminal("developer")

        assert "working_directory" not in captured["params"]


class TestSendHelpers:
    def test_send_direct_input_calls_terminal_input_endpoint(self, monkeypatch):
        captured = {}

        def _post(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _Response({"success": True})

        monkeypatch.setattr(server.requests, "post", _post)

        server._send_direct_input("abc123ef", "hello")

        assert captured["url"].endswith("/terminals/abc123ef/input")
        assert captured["params"] == {"message": "hello"}

    def test_send_to_inbox_requires_sender_env(self, monkeypatch):
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)

        with pytest.raises(ValueError, match="CAO_TERMINAL_ID not set"):
            server._send_to_inbox("recv1234", "hello")

    def test_send_to_inbox_calls_receiver_scoped_endpoint(self, monkeypatch):
        monkeypatch.setenv("CAO_TERMINAL_ID", "sender99")

        captured = {}

        def _post(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _Response({"success": True, "message_id": 1})

        monkeypatch.setattr(server.requests, "post", _post)

        result = server._send_to_inbox("recv1234", "queued")

        assert captured["url"].endswith("/terminals/recv1234/inbox/messages")
        assert captured["params"] == {"sender_id": "sender99", "message": "queued"}
        assert result["success"] is True


class TestHandoffImpl:
    @pytest.mark.asyncio
    async def test_handoff_happy_path(self, monkeypatch, patch_async_sleep):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("abc123ef", "q_cli"))
        monkeypatch.setattr(server, "wait_until_terminal_status", lambda *_args, **_kwargs: True)

        sent = []
        monkeypatch.setattr(server, "_send_direct_input", lambda tid, msg: sent.append((tid, msg)))

        get_calls = []

        def _get(url, params=None, timeout=None):
            get_calls.append((url, params))
            return _Response({"output": "final answer"})

        post_calls = []

        def _post(url, params=None, timeout=None):
            post_calls.append((url, params))
            return _Response({"success": True})

        monkeypatch.setattr(server.requests, "get", _get)
        monkeypatch.setattr(server.requests, "post", _post)

        result = await server._handoff_impl("developer", "do thing", timeout=5)

        assert result.success is True
        assert result.output == "final answer"
        assert result.terminal_id == "abc123ef"
        assert sent == [("abc123ef", "do thing")]
        assert any(url.endswith("/terminals/abc123ef/exit") for (url, _params) in post_calls)

    @pytest.mark.asyncio
    async def test_handoff_idle_timeout(self, monkeypatch, patch_async_sleep):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("abc123ef", "q_cli"))
        monkeypatch.setattr(server, "wait_until_terminal_status", lambda *_args, **_kwargs: False)

        result = await server._handoff_impl("developer", "do thing", timeout=5)

        assert result.success is False
        assert "did not reach IDLE" in result.message
        assert result.terminal_id == "abc123ef"

    @pytest.mark.asyncio
    async def test_handoff_completed_timeout(self, monkeypatch, patch_async_sleep):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("abc123ef", "q_cli"))

        calls = {"count": 0}

        def _wait(*_args, **_kwargs):
            calls["count"] += 1
            return calls["count"] == 1

        monkeypatch.setattr(server, "wait_until_terminal_status", _wait)
        monkeypatch.setattr(server, "_send_direct_input", lambda *_args, **_kwargs: None)

        result = await server._handoff_impl("developer", "do thing", timeout=5)

        assert result.success is False
        assert "timed out" in result.message
        assert result.terminal_id == "abc123ef"

    @pytest.mark.asyncio
    async def test_handoff_exception_during_terminal_creation(self, monkeypatch, patch_async_sleep):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

        result = await server._handoff_impl("developer", "do thing", timeout=5)

        assert result.success is False
        assert "Handoff failed" in result.message
        assert result.terminal_id is None

    @pytest.mark.asyncio
    async def test_handoff_exception_after_creation_returns_terminal_id_none(self, monkeypatch, patch_async_sleep):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("abc123ef", "q_cli"))
        monkeypatch.setattr(server, "wait_until_terminal_status", lambda *_args, **_kwargs: True)
        monkeypatch.setattr(server, "_send_direct_input", lambda *_args, **_kwargs: None)

        def _get(url, params=None, timeout=None):
            raise RuntimeError("output fetch failed")

        monkeypatch.setattr(server.requests, "get", _get)

        result = await server._handoff_impl("developer", "do thing", timeout=5)

        assert result.success is False
        assert result.terminal_id is None

    @pytest.mark.asyncio
    async def test_handoff_timeout_does_not_send_exit(self, monkeypatch, patch_async_sleep):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("abc123ef", "q_cli"))

        calls = {"count": 0}

        def _wait(*_args, **_kwargs):
            calls["count"] += 1
            return calls["count"] == 1

        monkeypatch.setattr(server, "wait_until_terminal_status", _wait)
        monkeypatch.setattr(server, "_send_direct_input", lambda *_args, **_kwargs: None)

        posted = []

        def _post(url, params=None, timeout=None):
            posted.append(url)
            return _Response({"success": True})

        monkeypatch.setattr(server.requests, "post", _post)

        result = await server._handoff_impl("developer", "do thing", timeout=5)

        assert result.success is False
        assert all(not url.endswith("/exit") for url in posted)


class TestAssignImpl:
    def test_assign_happy_path(self, monkeypatch):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: ("abc123ef", "q_cli"))
        sent = []
        monkeypatch.setattr(server, "_send_direct_input", lambda tid, msg: sent.append((tid, msg)))

        result = server._assign_impl("developer", "work")

        assert result["success"] is True
        assert result["terminal_id"] == "abc123ef"
        assert sent == [("abc123ef", "work")]

    def test_assign_exception_handling(self, monkeypatch):
        monkeypatch.setattr(server, "_create_terminal", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

        result = server._assign_impl("developer", "work")

        assert result["success"] is False
        assert result["terminal_id"] is None
        assert "Assignment failed" in result["message"]


class TestSendMessageTool:
    @pytest.mark.asyncio
    async def test_send_message_success(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_send_to_inbox",
            lambda receiver_id, message: {
                "success": True,
                "receiver_id": receiver_id,
                "message": message,
            },
        )

        result = await server.send_message.fn(receiver_id="abcd1234", message="hello")

        assert result["success"] is True
        assert result["receiver_id"] == "abcd1234"

    @pytest.mark.asyncio
    async def test_send_message_wraps_exception(self, monkeypatch):
        monkeypatch.setattr(
            server,
            "_send_to_inbox",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        result = await server.send_message.fn(receiver_id="abcd1234", message="hello")

        assert result["success"] is False
        assert "boom" in result["error"]
