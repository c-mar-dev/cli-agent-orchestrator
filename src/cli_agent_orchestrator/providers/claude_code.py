"""Claude Code provider implementation."""

import os
import re
import shlex
import uuid
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import CAO_JSONL_ENABLED
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.parsing.jsonl_status_engine import jsonl_status_engine
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_until_status


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


# Regex patterns for Claude Code output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
ANSI_ESCAPE_PATTERN = r"\x1b\[[0-9;?]*[A-Za-z]"
RESPONSE_PATTERN = r"[⏺●](?:\x1b\[[0-9;]*m)*\s+"  # Handle ⏺ and ● response markers
PROCESSING_PATTERN = r"[✶✢✽✻·✳](?:\x1b\[[0-9;]*m)*\s+(?:\x1b\[[0-9;]*m)*\S+(?:\x1b\[[0-9;]*m)*…"
IDLE_PROMPT_PATTERN = r"[>❯][\s\xa0]"  # Handle both > and ❯ prompt chars
WAITING_USER_ANSWER_PATTERN = (
    r"❯.*\d+\."  # Pattern for Claude showing selection options with arrow cursor
)
WAITING_USER_NAV_HINT_PATTERN = r"Enter\s+to\s+select"
IDLE_PROMPT_PATTERN_LOG = r"[>❯][\s\xa0]"  # Same pattern for log files


def _is_truthy(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class ClaudeCodeProvider(BaseProvider):
    """Provider for Claude Code CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile
        self._session_id_hint = str(uuid.uuid4())

    def _build_claude_command(self) -> str:
        """Build Claude Code command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses shlex.join() to handle multiline strings and special characters correctly.
        """
        command_parts = ["claude"]
        command_parts.extend(["--session-id", self._session_id_hint])

        permission_mode = os.getenv("CAO_CLAUDE_PERMISSION_MODE")
        if permission_mode:
            command_parts.extend(["--permission-mode", permission_mode])

        if _is_truthy(os.getenv("CAO_CLAUDE_DANGEROUS_SKIP_PERMISSIONS")):
            command_parts.append("--dangerously-skip-permissions")
        elif _is_truthy(os.getenv("CAO_CLAUDE_ALLOW_DANGEROUS_SKIP_PERMISSIONS")):
            command_parts.append("--allow-dangerously-skip-permissions")

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Add system prompt - escape newlines to prevent tmux chunking issues
                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
                if system_prompt:
                    # Replace actual newlines with \n escape sequences
                    # This prevents tmux send_keys chunking from breaking the command
                    escaped_prompt = system_prompt.replace("\\", "\\\\").replace("\n", "\\n")
                    command_parts.extend(["--append-system-prompt", escaped_prompt])

                # Add MCP config if present
                if profile.mcpServers:
                    mcp_json = profile.model_dump_json(include={"mcpServers"})
                    command_parts.extend(["--mcp-config", mcp_json])

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        # Use shlex.join() for proper shell escaping of all arguments
        # This correctly handles multiline strings, quotes, and special characters
        return shlex.join(command_parts)

    def initialize(self) -> bool:
        """Initialize Claude Code provider by starting claude command."""
        # Build properly escaped command string
        command = self._build_claude_command()

        # Prevent nested Claude session detection when CAO is launched inside Claude.
        command = f"unset CLAUDECODE && {command}"

        # Send Claude Code command using tmux client
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Wait for Claude Code prompt to be ready
        if not wait_until_status(
            self,
            TerminalStatus.IDLE,
            acceptable_statuses=(TerminalStatus.COMPLETED,),
            timeout=30.0,
            polling_interval=1.0,
        ):
            raise TimeoutError("Claude Code initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Claude Code status by analyzing terminal output."""
        if CAO_JSONL_ENABLED:
            jsonl_result = jsonl_status_engine.get_status(
                ProviderType.CLAUDE_CODE.value,
                self.terminal_id,
                self.session_name,
                self.window_name,
            )
            if jsonl_result.deterministic and jsonl_result.status is not None:
                self._update_status(jsonl_result.status)
                return jsonl_result.status

        status = self._get_status_from_tmux(tail_lines=tail_lines)
        self._update_status(status)
        return status

    def _get_status_from_tmux(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get status by parsing tmux output only."""
        # Use tmux client singleton to get window history
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        normalized_output = re.sub(ANSI_ESCAPE_PATTERN, "", output)
        normalized_output_lc = normalized_output.lower()

        # Check for waiting user answer (Claude asking for user selection)
        if re.search(WAITING_USER_ANSWER_PATTERN, normalized_output) and re.search(
            WAITING_USER_NAV_HINT_PATTERN, normalized_output
        ):
            return TerminalStatus.WAITING_USER_ANSWER
        if (
            "bypass permissions" in normalized_output_lc
            and "shift+tab to cycle" in normalized_output_lc
            and re.search(WAITING_USER_NAV_HINT_PATTERN, normalized_output)
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check for completed state (has response + ready prompt)
        if re.search(RESPONSE_PATTERN, output) and re.search(IDLE_PROMPT_PATTERN, output):
            return TerminalStatus.COMPLETED

        # Check for idle state (just ready prompt, no response)
        if re.search(IDLE_PROMPT_PATTERN, output):
            return TerminalStatus.IDLE

        # Check for processing state after ready checks to avoid stale glyph false-positives.
        if re.search(PROCESSING_PATTERN, output):
            return TerminalStatus.PROCESSING

        # If no recognizable state, return ERROR
        return TerminalStatus.ERROR

    def uses_jsonl_status(self) -> bool:
        """Claude can use JSONL status when enabled."""
        return CAO_JSONL_ENABLED

    def get_tmux_status(self, tail_lines: Optional[int] = None) -> Optional[TerminalStatus]:
        """Return tmux-only status for telemetry comparison."""
        return self._get_status_from_tmux(tail_lines=tail_lines)

    def get_idle_pattern_for_log(self) -> str:
        """Return Claude Code IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Claude's final response message using ⏺ indicator."""
        if CAO_JSONL_ENABLED:
            jsonl_result = jsonl_status_engine.get_status(
                ProviderType.CLAUDE_CODE.value,
                self.terminal_id,
                self.session_name,
                self.window_name,
            )
            if jsonl_result.deterministic and jsonl_result.last_message:
                return jsonl_result.last_message

        # Find all matches of response pattern
        matches = list(re.finditer(RESPONSE_PATTERN, script_output))

        if not matches:
            raise ValueError("No Claude Code response found - no ⏺ pattern detected")

        # Get the last match (final answer)
        last_match = matches[-1]
        start_pos = last_match.end()

        # Extract everything after the last ⏺ until next prompt or separator
        remaining_text = script_output[start_pos:]

        # Split by lines and extract response
        lines = remaining_text.split("\n")
        response_lines = []

        for line in lines:
            # Stop at next > or ❯ prompt or separator line
            if re.match(r"[>❯]\s", line) or "────────" in line:
                break

            # Clean the line
            clean_line = line.strip()
            response_lines.append(clean_line)

        if not response_lines or not any(line.strip() for line in response_lines):
            raise ValueError("Empty Claude Code response - no content found after ⏺")

        # Join lines and clean up
        final_answer = "\n".join(response_lines).strip()
        # Remove ANSI codes from the final message
        final_answer = re.sub(ANSI_CODE_PATTERN, "", final_answer)
        return final_answer.strip()

    def exit_cli(self) -> str:
        """Get the command to exit Claude Code."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Claude Code provider."""
        self._initialized = False

    def get_provider_session_hint(self) -> Optional[str]:
        """Return deterministic Claude session ID injected at launch."""
        return self._session_id_hint
