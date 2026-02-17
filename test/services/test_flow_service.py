"""Integration tests for flow service CRUD and execution."""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import flow_service


def _write_flow(
    tmp_path: Path,
    *,
    name: str,
    schedule: str = "* * * * *",
    agent_profile: str = "developer",
    provider: str | None = None,
    script: str | None = None,
    prompt: str = "Do the work",
) -> Path:
    lines = [
        "---",
        f"name: {name}",
        f'schedule: "{schedule}"',
        f"agent_profile: {agent_profile}",
    ]
    if provider is not None:
        lines.append(f"provider: {provider}")
    if script is not None:
        lines.append(f"script: {script}")
    lines.extend(["---", prompt])
    path = tmp_path / f"{name}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_script(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_add_flow_parses_frontmatter_and_stores_db(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = _write_flow(tmp_path, name="daily")

    flow = flow_service.add_flow(str(flow_file))

    assert flow.name == "daily"
    assert flow.file_path == str(flow_file)


def test_add_flow_missing_required_field_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = tmp_path / "invalid.md"
    flow_file.write_text(
        "---\nname: bad\nschedule: \"* * * * *\"\n---\nPrompt\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Missing required field"):
        flow_service.add_flow(str(flow_file))


def test_add_flow_invalid_cron_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = _write_flow(tmp_path, name="bad-cron", schedule="not-a-cron")

    with pytest.raises(ValueError, match="Invalid cron expression"):
        flow_service.add_flow(str(flow_file))


def test_add_flow_invalid_provider_is_rejected_with_allowed_set(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = _write_flow(tmp_path, name="weird-provider", provider="totally_unknown")

    with pytest.raises(ValueError, match="Allowed providers"):
        flow_service.add_flow(str(flow_file))


def test_list_get_remove_disable_enable_flow_lifecycle(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = _write_flow(tmp_path, name="lifecycle")
    flow_service.add_flow(str(flow_file))

    listed = flow_service.list_flows()
    assert [f.name for f in listed] == ["lifecycle"]

    fetched = flow_service.get_flow("lifecycle")
    assert fetched.name == "lifecycle"

    assert flow_service.disable_flow("lifecycle") is True
    assert flow_service.get_flow("lifecycle").enabled is False

    before_next_run = flow_service.get_flow("lifecycle").next_run
    assert flow_service.enable_flow("lifecycle") is True
    after_next_run = flow_service.get_flow("lifecycle").next_run
    assert after_next_run is not None
    assert before_next_run is None or after_next_run >= before_next_run

    assert flow_service.remove_flow("lifecycle") is True
    with pytest.raises(ValueError, match="not found"):
        flow_service.get_flow("lifecycle")


def test_get_nonexistent_flow_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
):
    with pytest.raises(ValueError, match="not found"):
        flow_service.get_flow("missing")


def test_execute_flow_without_script_creates_terminal_and_sends_prompt(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = _write_flow(tmp_path, name="no-script", prompt="Run static task")
    flow_service.add_flow(str(flow_file))

    executed = flow_service.execute_flow("no-script")

    assert executed is True
    assert any(keys == "Run static task" for (_s, _w, keys) in fake_tmux._keys_sent)


def test_execute_flow_with_script_renders_prompt(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    script = _write_script(
        tmp_path / "emit.sh",
        "#!/usr/bin/env bash\necho '{\"execute\": true, \"output\": {\"name\": \"Alice\"}}'\n",
    )
    flow_file = _write_flow(
        tmp_path,
        name="with-script",
        script="emit.sh",
        prompt="Hello [[name]]",
    )
    flow_service.add_flow(str(flow_file))

    executed = flow_service.execute_flow("with-script")

    assert executed is True
    assert any(keys == "Hello Alice" for (_s, _w, keys) in fake_tmux._keys_sent)


def test_execute_flow_script_execute_false_skips_terminal_launch(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    _write_script(
        tmp_path / "skip.sh",
        "#!/usr/bin/env bash\necho '{\"execute\": false, \"output\": {}}'\n",
    )
    flow_file = _write_flow(
        tmp_path,
        name="skip-flow",
        script="skip.sh",
        prompt="Should not run",
    )
    flow_service.add_flow(str(flow_file))

    executed = flow_service.execute_flow("skip-flow")

    assert executed is False
    assert all(keys != "Should not run" for (_s, _w, keys) in fake_tmux._keys_sent)


def test_execute_flow_script_nonzero_exit_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    _write_script(
        tmp_path / "fail.sh",
        "#!/usr/bin/env bash\necho 'boom' 1>&2\nexit 7\n",
    )
    flow_file = _write_flow(tmp_path, name="fail-flow", script="fail.sh")
    flow_service.add_flow(str(flow_file))

    with pytest.raises(ValueError, match="Script failed with exit code"):
        flow_service.execute_flow("fail-flow")


def test_execute_flow_missing_script_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = _write_flow(tmp_path, name="missing-script", script="does-not-exist.sh")
    flow_service.add_flow(str(flow_file))

    with pytest.raises(ValueError, match="Script not found"):
        flow_service.execute_flow("missing-script")


def test_execute_flow_script_timeout_raises(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
    monkeypatch,
):
    flow_file = _write_flow(tmp_path, name="timeout-flow", script="noop.sh")
    _write_script(tmp_path / "noop.sh", "#!/usr/bin/env bash\necho '{}'\n")
    flow_service.add_flow(str(flow_file))

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="noop.sh", timeout=30)

    monkeypatch.setattr(flow_service.subprocess, "run", _timeout)

    with pytest.raises(subprocess.TimeoutExpired):
        flow_service.execute_flow("timeout-flow")


def test_execute_flow_script_invalid_json_shape_missing_execute(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    _write_script(
        tmp_path / "bad-shape.sh",
        "#!/usr/bin/env bash\necho '{\"output\": {\"x\": 1}}'\n",
    )
    flow_file = _write_flow(tmp_path, name="bad-shape", script="bad-shape.sh")
    flow_service.add_flow(str(flow_file))

    with pytest.raises(ValueError, match="missing 'execute' field"):
        flow_service.execute_flow("bad-shape")


def test_execute_flow_script_invalid_json_shape_missing_output(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    _write_script(
        tmp_path / "bad-shape2.sh",
        "#!/usr/bin/env bash\necho '{\"execute\": true}'\n",
    )
    flow_file = _write_flow(tmp_path, name="bad-shape2", script="bad-shape2.sh")
    flow_service.add_flow(str(flow_file))

    with pytest.raises(ValueError, match="missing 'output' field"):
        flow_service.execute_flow("bad-shape2")


def test_execute_flow_script_output_must_be_dict(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    _write_script(
        tmp_path / "bad-output-type.sh",
        "#!/usr/bin/env bash\necho '{\"execute\": true, \"output\": \"not-a-dict\"}'\n",
    )
    flow_file = _write_flow(tmp_path, name="bad-output-type", script="bad-output-type.sh")
    flow_service.add_flow(str(flow_file))

    with pytest.raises(ValueError, match="must be a dictionary"):
        flow_service.execute_flow("bad-output-type")


def test_execute_flow_renders_template_variables(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    _write_script(
        tmp_path / "vars.sh",
        "#!/usr/bin/env bash\necho '{\"execute\": true, \"output\": {\"title\": \"Release\", \"count\": 3}}'\n",
    )
    flow_file = _write_flow(
        tmp_path,
        name="vars",
        script="vars.sh",
        prompt="[[title]] has [[count]] items",
    )
    flow_service.add_flow(str(flow_file))

    executed = flow_service.execute_flow("vars")

    assert executed is True
    assert any(keys == "Release has 3 items" for (_s, _w, keys) in fake_tmux._keys_sent)


def test_get_flows_to_run_filters_enabled_and_past_due(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    due_file = _write_flow(tmp_path, name="due")
    not_due_file = _write_flow(tmp_path, name="future")
    disabled_file = _write_flow(tmp_path, name="disabled")

    flow_service.add_flow(str(due_file))
    flow_service.add_flow(str(not_due_file))
    flow_service.add_flow(str(disabled_file))

    from cli_agent_orchestrator.clients import database

    now = datetime.now()
    database.update_flow_run_times("due", last_run=now - timedelta(hours=1), next_run=now - timedelta(minutes=1))
    database.update_flow_run_times(
        "future",
        last_run=now - timedelta(hours=1),
        next_run=now + timedelta(minutes=20),
    )
    database.update_flow_run_times(
        "disabled",
        last_run=now - timedelta(hours=1),
        next_run=now - timedelta(minutes=1),
    )
    flow_service.disable_flow("disabled")

    names = sorted(f.name for f in flow_service.get_flows_to_run())
    assert names == ["due"]


def test_get_flows_to_run_excludes_disabled_even_when_due(
    in_memory_db,
    fake_tmux,
    fake_provider_manager,
    terminal_log_dir,
    tmp_path,
):
    flow_file = _write_flow(tmp_path, name="disabled-due")
    flow_service.add_flow(str(flow_file))

    from cli_agent_orchestrator.clients import database

    now = datetime.now()
    database.update_flow_run_times(
        "disabled-due",
        last_run=now - timedelta(hours=1),
        next_run=now - timedelta(minutes=1),
    )
    flow_service.disable_flow("disabled-due")

    assert flow_service.get_flows_to_run() == []
