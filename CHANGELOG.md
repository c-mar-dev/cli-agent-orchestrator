# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Inbox DB telemetry counters exposed via `GET /diagnostics/inbox/telemetry` (`db` section).
- Runtime profile default `inbox.requeue_terminal_state_default` / `CAO_INBOX_REQUEUE_TERMINAL_STATE_DEFAULT`.
- Repo-local session log convention docs under `docs/session-logs/`.

### Changed

- Inbox idempotency creation path is now conflict-safe (`INSERT OR IGNORE` + canonical fetch).
- Flow creation now validates provider values against supported provider list.
- Session deletion now uses terminal-level delete path, preserving approval cleanup behavior.
- API docs and runtime profile docs aligned with current endpoint and contract surface.

### Fixed

- Enforced unique inbox idempotency key per receiver with deterministic duplicate-pruning migration.
- Added explicit dead-letter/failed requeue semantics via `requeue_terminal_state`.
- Terminal deletion now attempts pending approval resolution even when DB delete errors.
- Orchestration test bridge in `test/orchestration/test_handoff_flow.py` now supports timeout kwargs and updated `wait_until_terminal_status` kwargs.

### Verification

- `uv run pytest -q test/orchestration/test_handoff_flow.py`
- `uv run pytest -q -m "not e2e"` -> `399 passed, 16 skipped, 5 deselected`
- `uv run python -m compileall -q src`

## [1.0.0] - 2026-01-23

### Added

- async delegate (#3)

- add badge to deepwiki for weekly auto-refresh (#13)

- add Codex CLI provider (#39)


### Changed

- rename 'delegate' to 'assign' throughout codebase (#10)


### Fixed

- Handle percentage in agent prompt pattern (#4)

- resolve code formatting issues in upstream main (#40)


### Other

- Initial commit

- Initial Launch (#1)

- Inbox Service (#2)

- tmux install script (#5)

- update README: orchestration modes (#6)

- Update README.md (#7)

- Update issue templates (#8)

- Document update with Mermaid process diagram (#9)

- Adding examples for assign (async parallel) (#11)

- update idle prompt pattern for Q CLI to use consistent color codes (#15)

- Add comprehensive test suite for Q CLI provider (#16)

- Add code formatting and type checking with Black, isort, and mypy (#20)

- Make Q CLI Prompt Pattern Matching ANSI color-agnostic (#18)

- Add explicit permissions to workflow

- Kiro CLI provider (#25)

- Add GET endpoint for inbox messages with status filtering (#30)

- Adding git to the install dependencies message (#28)

- Bump to v0.51.0, update method name (#31)

- accept optional U+03BB (λ) after % in kiro and q CLIs (#44)

