#!/usr/bin/env python3
"""Execute the master JSONL rollout smoke procedure and emit a strict report."""

from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


DEFAULT_MESSAGE = "Ask me exactly one multiple-choice question (3 options), then stop and wait for my answer."
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run master JSONL rollout smoke test")
    parser.add_argument("--base-url", default="http://127.0.0.1:9893")
    parser.add_argument("--session-name", default="cao-jsonl-rollout")
    parser.add_argument("--provider", default="claude_code")
    parser.add_argument("--agent-profile", default="pipe-orch")
    parser.add_argument("--working-directory", default="/home/charl/projects/ORCH-SYSTEM")
    parser.add_argument("--message", default=DEFAULT_MESSAGE)
    parser.add_argument("--poll-interval-seconds", type=float, default=2.0)
    parser.add_argument("--poll-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--request-timeout-seconds", type=float, default=30.0)
    parser.add_argument(
        "--reset-session",
        action="store_true",
        help="Delete the rollout session before running the smoke test",
    )
    parser.add_argument(
        "--out-json",
        default="work/canary-snapshots/master-rollout-smoke-latest.json",
        help="Report output path",
    )
    return parser.parse_args()


def _url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}{path}"


def _get_json(base_url: str, path: str, timeout_seconds: float) -> Dict[str, Any]:
    response = requests.get(_url(base_url, path), timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object for {path}, got {type(payload)}")
    return payload


def _post_json(
    base_url: str,
    path: str,
    timeout_seconds: float,
    *,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    response = requests.post(_url(base_url, path), params=params, timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object for {path}, got {type(payload)}")
    return payload


@dataclass
class WaitingCheck:
    passed: bool
    condition: Optional[str]
    evidence: str


def _detect_mcq(output_text: str) -> WaitingCheck:
    if not output_text:
        return WaitingCheck(False, None, "empty_output")

    # Terminal output can include ANSI color/control escapes; strip before pattern checks.
    output_text = ANSI_ESCAPE_RE.sub("", output_text)
    lines = output_text.splitlines()
    tail = lines[-200:] if len(lines) > 200 else lines
    normalized = "\n".join(tail)

    has_question = "?" in normalized
    option_patterns = [
        r"(?m)^\s*1[\)\.\:]\s+\S",
        r"(?m)^\s*2[\)\.\:]\s+\S",
        r"(?m)^\s*3[\)\.\:]\s+\S",
        r"(?m)^\s*A[\)\.\:]\s+\S",
        r"(?m)^\s*B[\)\.\:]\s+\S",
        r"(?m)^\s*C[\)\.\:]\s+\S",
    ]
    matches = sum(bool(re.search(pat, normalized)) for pat in option_patterns)
    has_three_options = matches >= 3

    if has_question and has_three_options:
        return WaitingCheck(True, "B", "mcq_pattern_detected")
    return WaitingCheck(False, None, "mcq_pattern_not_detected")


def _fetch_terminal(base_url: str, terminal_id: str, timeout_seconds: float) -> Dict[str, Any]:
    return _get_json(base_url, f"/terminals/{terminal_id}", timeout_seconds)


def _fetch_output(base_url: str, terminal_id: str, timeout_seconds: float) -> str:
    payload = _get_json(base_url, f"/terminals/{terminal_id}/output?mode=full", timeout_seconds)
    value = payload.get("output", "")
    if value is None:
        return ""
    return str(value)


def resolve_rollout_terminal(
    *,
    base_url: str,
    session_name: str,
    provider: str,
    agent_profile: str,
    working_directory: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    session_created = False
    session_payload: Optional[Dict[str, Any]] = None
    response = requests.get(_url(base_url, f"/sessions/{session_name}"), timeout=timeout_seconds)
    if response.status_code == 404:
        created_terminal = _post_json(
            base_url,
            "/sessions",
            timeout_seconds,
            params={
                "provider": provider,
                "agent_profile": agent_profile,
                "session_name": session_name,
                "working_directory": working_directory,
            },
        )
        session_created = True
        terminal_id = str(created_terminal["id"])
        return {
            "session_created": session_created,
            "terminal_id": terminal_id,
            "session_payload": None,
            "terminal_created_in_existing_session": False,
        }

    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected /sessions/{{session}} payload type: {type(payload)}")
    session_payload = payload
    terminals = payload.get("terminals") or []
    if not isinstance(terminals, list):
        raise ValueError("Expected list in session terminals")

    for terminal in terminals:
        if not isinstance(terminal, dict):
            continue
        if str(terminal.get("provider")) == provider and terminal.get("id"):
            return {
                "session_created": session_created,
                "terminal_id": str(terminal["id"]),
                "session_payload": session_payload,
                "terminal_created_in_existing_session": False,
            }

    if terminals:
        for terminal in terminals:
            if isinstance(terminal, dict) and terminal.get("id"):
                return {
                    "session_created": session_created,
                    "terminal_id": str(terminal["id"]),
                    "session_payload": session_payload,
                    "terminal_created_in_existing_session": False,
                }

    created_terminal = _post_json(
        base_url,
        f"/sessions/{session_name}/terminals",
        timeout_seconds,
        params={
            "provider": provider,
            "agent_profile": agent_profile,
            "working_directory": working_directory,
        },
    )
    return {
        "session_created": session_created,
        "terminal_id": str(created_terminal["id"]),
        "session_payload": session_payload,
        "terminal_created_in_existing_session": True,
    }


def main() -> int:
    args = parse_args()

    report: Dict[str, Any] = {
        "started_at": utc_now(),
        "inputs": {
            "base_url": args.base_url,
            "session_name": args.session_name,
            "provider": args.provider,
            "agent_profile": args.agent_profile,
            "working_directory": args.working_directory,
            "poll_interval_seconds": args.poll_interval_seconds,
            "poll_timeout_seconds": args.poll_timeout_seconds,
        },
        "why_what_summary": [
            "This drill verifies JSONL-driven status is reliable enough to reduce tmux/ANSI parsing fragility.",
            "It checks watcher readiness, then drives a real agent interaction to validate waiting and mapping behavior.",
            "Pass requires JSONL source + usable confidence and a detected waiting state.",
            "Fail returns explicit blocker reason codes so rollout decisions are deterministic.",
        ],
        "preflight": {},
        "resolution": {},
        "poll_trace": [],
    }

    reason_codes: List[str] = []
    failed_step: Optional[str] = None

    # 1) Preflight
    try:
        health = _get_json(args.base_url, "/health", args.request_timeout_seconds)
        telemetry = _get_json(
            args.base_url, "/diagnostics/inbox/telemetry", args.request_timeout_seconds
        )
        health_ok = str(health.get("status", "")).lower() == "ok"
        watch_enabled = bool(telemetry.get("jsonl_watch_enabled", False))
        watcher_active = bool(telemetry.get("jsonl_watcher_active", False))
        preflight_ok = health_ok and watch_enabled and watcher_active
        report["preflight"] = {
            "ok": preflight_ok,
            "health_status": health.get("status"),
            "jsonl_watch_enabled": watch_enabled,
            "jsonl_watcher_active": watcher_active,
            "telemetry_excerpt": {
                "jsonl_watcher_events_received": telemetry.get("jsonl_watcher_events_received"),
                "last_jsonl_event_at": telemetry.get("last_jsonl_event_at"),
                "last_jsonl_event_path": telemetry.get("last_jsonl_event_path"),
            },
        }
        if not preflight_ok:
            failed_step = "1_preflight_health_and_watcher"
            if not health_ok:
                reason_codes.append("health_not_ok")
            if not (watch_enabled and watcher_active):
                reason_codes.append("watcher_not_ready")
    except Exception as exc:
        failed_step = "1_preflight_health_and_watcher"
        reason_codes.append("preflight_request_failed")
        report["preflight"] = {"ok": False, "error": str(exc)}

    terminal_id: Optional[str] = None
    resolution_info: Dict[str, Any] = {}
    waiting_passed = False
    waiting_condition: Optional[str] = None
    waiting_evidence = ""
    mapping_passed = False
    first_jsonl_at: Optional[str] = None
    final_mapping_confidence: Optional[str] = None

    # 2) Resolve rollout terminal
    if failed_step is None:
        try:
            if args.reset_session:
                # Best-effort cleanup to avoid stale long-running turns affecting smoke runs.
                requests.delete(
                    _url(args.base_url, f"/sessions/{args.session_name}"),
                    timeout=args.request_timeout_seconds,
                )
            resolution_info = resolve_rollout_terminal(
                base_url=args.base_url,
                session_name=args.session_name,
                provider=args.provider,
                agent_profile=args.agent_profile,
                working_directory=args.working_directory,
                timeout_seconds=args.request_timeout_seconds,
            )
            terminal_id = str(resolution_info["terminal_id"])
            report["resolution"] = resolution_info
            report["resolution"]["terminal_id"] = terminal_id
        except Exception as exc:
            failed_step = "2_resolve_rollout_terminal"
            reason_codes.append("session_resolution_failed")
            report["resolution"] = {"ok": False, "error": str(exc)}

    # 3) Stimulate waiting state + 4) Confirm JSONL mapping/source
    if failed_step is None and terminal_id:
        try:
            _post_json(
                args.base_url,
                f"/terminals/{terminal_id}/input",
                args.request_timeout_seconds,
                params={"message": args.message},
            )
        except Exception as exc:
            failed_step = "3_stimulate_waiting_state"
            reason_codes.append("send_input_failed")
            report["send_input_error"] = str(exc)

    if failed_step is None and terminal_id:
        deadline = time.time() + max(args.poll_timeout_seconds, 1.0)
        attempts = 0
        while time.time() <= deadline:
            attempts += 1
            captured_at = utc_now()
            terminal_payload = _fetch_terminal(args.base_url, terminal_id, args.request_timeout_seconds)
            status = str(terminal_payload.get("status", ""))
            status_source = str(terminal_payload.get("status_source") or "")
            mapping_confidence = str(terminal_payload.get("mapping_confidence") or "")
            status_reason_code = terminal_payload.get("status_reason_code")
            report["poll_trace"].append(
                {
                    "captured_at": captured_at,
                    "attempt": attempts,
                    "status": status,
                    "status_source": status_source,
                    "mapping_confidence": mapping_confidence,
                    "status_reason_code": status_reason_code,
                }
            )

            if status_source == "jsonl" and mapping_confidence in {"medium", "high"}:
                mapping_passed = True
                final_mapping_confidence = mapping_confidence
                if first_jsonl_at is None:
                    first_jsonl_at = captured_at

            if status == "waiting_user_answer":
                waiting_passed = True
                waiting_condition = "A"
                waiting_evidence = "status_waiting_user_answer"
                if mapping_passed:
                    break

            if status == "error":
                failed_step = "3_stimulate_waiting_state"
                reason_codes.append("terminal_error")
                break

            if attempts % 3 == 0:
                output_text = _fetch_output(args.base_url, terminal_id, args.request_timeout_seconds)
                check = _detect_mcq(output_text)
                if check.passed:
                    waiting_passed = True
                    waiting_condition = check.condition
                    waiting_evidence = check.evidence
                    if mapping_passed:
                        break

            time.sleep(max(args.poll_interval_seconds, 0.1))

        if not waiting_passed and failed_step is None:
            output_text = _fetch_output(args.base_url, terminal_id, args.request_timeout_seconds)
            check = _detect_mcq(output_text)
            waiting_passed = check.passed
            waiting_condition = check.condition
            waiting_evidence = check.evidence

        if final_mapping_confidence is None and report["poll_trace"]:
            final_mapping_confidence = report["poll_trace"][-1].get("mapping_confidence")

        if failed_step is None and not waiting_passed:
            failed_step = "3_stimulate_waiting_state"
            reason_codes.append("waiting_state_not_observed")
        if failed_step is None and not mapping_passed:
            failed_step = "4_confirm_jsonl_mapping_source"
            reason_codes.append("jsonl_mapping_not_reliable")

    # 5) Collect diagnostics snapshot
    diagnostics: Dict[str, Any] = {}
    try:
        inbox_telemetry = _get_json(
            args.base_url, "/diagnostics/inbox/telemetry", args.request_timeout_seconds
        )
        gates_payload = _get_json(args.base_url, "/diagnostics/jsonl/gates", args.request_timeout_seconds)
        canary_gate = gates_payload.get("canary_gate") if isinstance(gates_payload, dict) else None
        gate_blockers = []
        if isinstance(canary_gate, dict):
            blockers = canary_gate.get("blockers", [])
            if isinstance(blockers, list):
                gate_blockers = blockers

        diagnostics = {
            "inbox_telemetry": inbox_telemetry,
            "jsonl_gates": gates_payload,
            "gate_blockers": gate_blockers,
        }
    except Exception as exc:
        reason_codes.append("diagnostics_snapshot_failed")
        diagnostics = {"error": str(exc)}

    passed = failed_step is None and not reason_codes
    if not passed and failed_step is None:
        failed_step = "unknown"

    report["result"] = {
        "preflight_ok": bool(report.get("preflight", {}).get("ok")),
        "terminal_id": terminal_id,
        "observed_statuses_over_time": report.get("poll_trace", []),
        "first_timestamp_status_source_jsonl": first_jsonl_at,
        "final_mapping_confidence": final_mapping_confidence,
        "waiting_condition": {
            "passed": waiting_passed,
            "condition": waiting_condition,
            "evidence": waiting_evidence,
        },
        "gate_blockers": diagnostics.get("gate_blockers", []),
        "pass_fail": "PASS" if passed else "FAIL",
        "reason_codes": sorted(set(reason_codes)),
        "failed_step": failed_step,
    }
    report["diagnostics"] = diagnostics
    report["finished_at"] = utc_now()

    out_path = Path(args.out_json)
    if not out_path.is_absolute():
        out_path = Path.cwd() / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(json.dumps(report["result"], indent=2))
    print(f"\nreport_path: {out_path}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
