"""Codex CLI provider implementation."""

import logging
import os
import re
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import CAO_JSONL_ENABLED
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.parsing.jsonl_status_engine import jsonl_status_engine
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Regex patterns for Codex output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
IDLE_PROMPT_PATTERN = r"(?:❯|›|codex>)"
# Match the prompt only if it appears at the end of the captured output.
# Allows trailing text on the same line (e.g., "What would you like to do next?")
IDLE_PROMPT_AT_END_PATTERN = rf"(?:^\s*{IDLE_PROMPT_PATTERN}\s*)\s*\Z"
IDLE_PROMPT_PATTERN_LOG = r"❯"
ASSISTANT_PREFIX_PATTERN = r"^(?:assistant|codex|agent)\s*:"
USER_PREFIX_PATTERN = r"^You\b"

PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
TRUST_PROMPT_PATTERN = r"Do you trust the contents of this directory|Press enter to continue"
STARTUP_READY_HINT_PATTERN = (
    r"Run /review on my current changes"
    r"|\? for shortcuts"
    r"|context left"
    r"|OpenAI Codex v[0-9]"
    r"|session id:\s*[0-9a-fA-F-]{8,}"
)
SESSION_ID_CAPTURE_PATTERN = re.compile(
    r"\bsession id:\s*([0-9a-fA-F]{8}-[0-9a-fA-F-]{27,})\b",
    re.IGNORECASE,
)


def _history_to_text(output: object) -> str:
    """Normalize tmux history to text for regex parsing."""
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    if isinstance(output, str):
        return output
    return str(output)


class CodexProvider(BaseProvider):
    """Provider for Codex CLI tool integration."""

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
        self._session_id_hint: Optional[str] = None

    def initialize(self) -> bool:
        """Initialize Codex provider by starting codex command."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        tmux_client.send_keys(self.session_name, self.window_name, "codex")
        init_timeout = float(os.getenv("CAO_CODEX_INIT_TIMEOUT", "60"))

        if not wait_until_status(
            self, TerminalStatus.IDLE, timeout=init_timeout, polling_interval=1.0
        ):
            startup_output = _history_to_text(
                tmux_client.get_history(self.session_name, self.window_name, tail_lines=200)
            )
            clean_output = re.sub(ANSI_CODE_PATTERN, "", startup_output)

            if re.search(TRUST_PROMPT_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
                raise TimeoutError(
                    "Codex initialization blocked by trust prompt. "
                    "Mark the working directory as trusted in ~/.codex/config.toml."
                )

            if re.search(STARTUP_READY_HINT_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
                logger.warning(
                    "Codex startup remained non-idle after %.0fs, but ready hints were detected; "
                    "continuing initialization.",
                    init_timeout,
                )
            else:
                raise TimeoutError(
                    f"Codex initialization timed out after {int(init_timeout)} seconds"
                )
            self._session_id_hint = self._extract_session_id_hint(clean_output)
        else:
            startup_output = _history_to_text(
                tmux_client.get_history(self.session_name, self.window_name, tail_lines=200)
            )
            clean_output = re.sub(ANSI_CODE_PATTERN, "", startup_output)
            self._session_id_hint = self._extract_session_id_hint(clean_output)

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Codex status by analyzing terminal output."""
        if CAO_JSONL_ENABLED:
            jsonl_result = jsonl_status_engine.get_status(
                ProviderType.CODEX.value,
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
        output = _history_to_text(
            tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
        )

        if not output:
            return TerminalStatus.ERROR

        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
        tail_output = "\n".join(clean_output.splitlines()[-25:])
        has_startup_ready_hint = bool(
            re.search(STARTUP_READY_HINT_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )

        if re.search(TRUST_PROMPT_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            return TerminalStatus.WAITING_USER_ANSWER

        last_user = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            last_user = match

        output_after_last_user = clean_output[last_user.start() :] if last_user else clean_output
        assistant_after_last_user = bool(
            last_user
            and re.search(
                ASSISTANT_PREFIX_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            )
        )

        has_idle_prompt_at_end = bool(
            re.search(IDLE_PROMPT_AT_END_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )

        # Only treat ERROR/WAITING prompts as actionable if they appear after the last user message
        # and are not part of an assistant response.
        if last_user is not None:
            if not assistant_after_last_user:
                if re.search(
                    WAITING_PROMPT_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(
                    ERROR_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR

        if last_user is None and has_startup_ready_hint:
            return TerminalStatus.IDLE

        if has_idle_prompt_at_end:
            # Consider COMPLETED only if we see an assistant marker after the last user message.
            if last_user is not None:
                if re.search(
                    ASSISTANT_PREFIX_PATTERN,
                    clean_output[last_user.start() :],
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.COMPLETED

                return TerminalStatus.IDLE

            return TerminalStatus.IDLE

        # If we're not at an idle prompt and we don't see explicit errors/permission prompts,
        # assume the CLI is still producing output.
        return TerminalStatus.PROCESSING

    def uses_jsonl_status(self) -> bool:
        """Codex can use JSONL status when enabled."""
        return CAO_JSONL_ENABLED

    def get_tmux_status(self, tail_lines: Optional[int] = None) -> Optional[TerminalStatus]:
        """Return tmux-only status for telemetry comparison."""
        return self._get_status_from_tmux(tail_lines=tail_lines)

    def get_idle_pattern_for_log(self) -> str:
        """Return Codex IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Codex's final response message using assistant label markers."""
        if CAO_JSONL_ENABLED:
            jsonl_result = jsonl_status_engine.get_status(
                ProviderType.CODEX.value,
                self.terminal_id,
                self.session_name,
                self.window_name,
            )
            if jsonl_result.deterministic and jsonl_result.last_message:
                return jsonl_result.last_message

        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )

        if not matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_match = matches[-1]
        start_pos = last_match.end()

        idle_after = re.search(
            IDLE_PROMPT_AT_END_PATTERN,
            clean_output[start_pos:],
            re.IGNORECASE | re.MULTILINE,
        )
        end_pos = start_pos + idle_after.start() if idle_after else len(clean_output)

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Codex response - no content found")

        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit Codex CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Codex CLI provider."""
        self._initialized = False

    def get_provider_session_hint(self) -> Optional[str]:
        """Return Codex session ID observed in startup banner, when available."""
        return self._session_id_hint

    @staticmethod
    def _extract_session_id_hint(output: str) -> Optional[str]:
        match = SESSION_ID_CAPTURE_PATTERN.search(output or "")
        if not match:
            return None
        return match.group(1).lower()
