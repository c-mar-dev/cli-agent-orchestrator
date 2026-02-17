"""CLI tests for flow commands."""

from __future__ import annotations

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.flow import flow


def test_flow_add_invalid_provider_surfaces_validation_error(in_memory_db, tmp_path):
    flow_file = tmp_path / "invalid-provider.md"
    flow_file.write_text(
        "\n".join(
            [
                "---",
                "name: invalid-provider",
                'schedule: "* * * * *"',
                "agent_profile: developer",
                "provider: not-real",
                "---",
                "Run this",
                "",
            ]
        ),
        encoding="utf-8",
    )

    runner = CliRunner()
    result = runner.invoke(flow, ["add", str(flow_file)])

    assert result.exit_code != 0
    assert "Invalid provider 'not-real'" in result.output
    assert "Allowed providers:" in result.output
