# Session Logs

Use this directory for engineering session handoff logs.

## Naming Convention

- `YYYY-MM-DD-[type]-[slug].md`
- `type` should be one of:
  - `dev` (feature/build work)
  - `debug` (investigation/fix work)
  - `mixed` (substantial dev + debug)

Example:
- `2026-02-17-mixed-caro-full-remediation-and-agent-review.md`

## Required Sections

Each session log should include:
- Session Overview
- System Context
- Changes Made
- Debug & Problem-Solving (when relevant)
- Lessons Learned
- Outstanding Issues (with `AGENT-TODO` items)
- Next Steps
- Metadata
- AGENTS.md update evaluation

## AGENT-TODO Format

Use:
- `**AGENT-TODO**: [PRIORITY] Description - Context`
- Priorities: `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`

## Template

Start from:
- `docs/session-logs/session-log-template.md`
