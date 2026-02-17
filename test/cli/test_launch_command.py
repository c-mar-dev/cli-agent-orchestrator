"""Tests for launch command defaults and overrides."""

from __future__ import annotations

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import launch as launch_cmd


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_launch_uses_default_working_directory(monkeypatch):
    monkeypatch.setattr(launch_cmd, "DEFAULT_WORKING_DIRECTORY", "/tmp/from-profile")

    captured = {}

    def _post(_url, params):
        captured.update(params)
        return _Response({"session_name": "cao-test", "name": "dev-1"})

    monkeypatch.setattr(launch_cmd.requests, "post", _post)

    runner = CliRunner()
    result = runner.invoke(launch_cmd.launch, ["--agents", "developer", "--headless"])

    assert result.exit_code == 0
    assert captured["working_directory"] == "/tmp/from-profile"


def test_launch_explicit_working_directory_overrides_default(monkeypatch):
    monkeypatch.setattr(launch_cmd, "DEFAULT_WORKING_DIRECTORY", "/tmp/from-profile")

    captured = {}

    def _post(_url, params):
        captured.update(params)
        return _Response({"session_name": "cao-test", "name": "dev-1"})

    monkeypatch.setattr(launch_cmd.requests, "post", _post)

    runner = CliRunner()
    result = runner.invoke(
        launch_cmd.launch,
        [
            "--agents",
            "developer",
            "--headless",
            "--working-directory",
            "/tmp/explicit",
        ],
    )

    assert result.exit_code == 0
    assert captured["working_directory"] == "/tmp/explicit"
