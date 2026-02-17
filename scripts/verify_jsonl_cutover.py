#!/usr/bin/env python3
"""Run a bounded JSONL cutover verification window and emit go/no-go signals.

This script evaluates runtime telemetry rather than unit tests:
- inbox telemetry: /diagnostics/inbox/telemetry
- gate snapshot:   /diagnostics/jsonl/gates
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests


def _default_probe_working_directory() -> str:
    script_path = Path(__file__).resolve()
    if len(script_path.parents) >= 3 and script_path.parents[1].name == "CARO-FORK":
        return str(script_path.parents[2])
    return str(Path.cwd())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify CAO JSONL cutover readiness")
    parser.add_argument("--base-url", default="http://127.0.0.1:9893")
    parser.add_argument("--duration-seconds", type=int, default=900)
    parser.add_argument("--interval-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--min-comparisons", type=int, default=50)
    parser.add_argument("--min-jsonl-events", type=int, default=5)
    parser.add_argument("--min-deliveries", type=int, default=0)
    parser.add_argument("--max-disagreement-rate", type=float, default=0.01)
    parser.add_argument(
        "--enforce-tmux-disagreement",
        action="store_true",
        help="Treat tmux disagreement rate as a hard blocker (default: advisory only)",
    )
    parser.add_argument("--max-fallback-rate", type=float, default=0.05)
    parser.add_argument("--max-watcher-error-rate", type=float, default=0.001)
    parser.add_argument(
        "--reset-before-window",
        action="store_true",
        help="Reset JSONL/inbox telemetry counters before sampling window",
    )
    parser.add_argument(
        "--out-json",
        default="work/canary-snapshots/cutover-verification-latest.json",
        help="Output report JSON path",
    )
    parser.add_argument(
        "--delivery-probe",
        action="store_true",
        help="Alias for --delivery-probe-mode=on.",
    )
    parser.add_argument(
        "--delivery-probe-mode",
        choices=("auto", "on", "off"),
        default="auto",
        help=(
            "Probe mode: auto enables traffic generation when min-deliveries > 0, "
            "on always enables, off disables."
        ),
    )
    parser.add_argument(
        "--delivery-probe-terminal-id",
        default="",
        help="Terminal ID to use for probe traffic (overrides session name).",
    )
    parser.add_argument(
        "--delivery-probe-session-name",
        default="",
        help="Session name whose first terminal is used for probe traffic when terminal-id is unset.",
    )
    parser.add_argument(
        "--delivery-probe-auto-session-name",
        default="cao-jsonl-rollout",
        help="Fallback session name used when probe target is not explicitly provided.",
    )
    parser.add_argument(
        "--delivery-probe-create-session",
        action="store_true",
        help="Allow verifier to create the auto probe session when it does not exist (off by default).",
    )
    parser.add_argument(
        "--delivery-probe-provider",
        default="claude_code",
        help="Provider used when creating the auto probe session.",
    )
    parser.add_argument(
        "--delivery-probe-agent-profile",
        default="pipe-orch",
        help="Agent profile used when creating the auto probe session.",
    )
    parser.add_argument(
        "--delivery-probe-working-directory",
        default=_default_probe_working_directory(),
        help="Working directory used when creating the auto probe session.",
    )
    parser.add_argument("--delivery-probe-message-interval-seconds", type=float, default=2.5)
    parser.add_argument("--delivery-probe-prompt-interval-seconds", type=float, default=15.0)
    parser.add_argument(
        "--delivery-probe-disable-prompts",
        action="store_true",
        help="Send only inbox messages in probe mode (skip synthetic input prompts).",
    )
    parser.add_argument("--request-retries", type=int, default=3)
    parser.add_argument(
        "--skip-remediation-before-window",
        action="store_true",
        help="Skip running remediate_jsonl_blockers.py before telemetry reset/window sampling.",
    )
    parser.add_argument(
        "--remediation-timeout-seconds",
        type=float,
        default=45.0,
        help="Timeout for remediation script execution.",
    )
    return parser.parse_args()


def _request_json_with_retries(
    method: str,
    base_url: str,
    path: str,
    timeout_seconds: float,
    retries: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    last_exc: Exception | None = None
    for attempt in range(max(retries, 1)):
        try:
            response = requests.request(method, url, timeout=timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Unexpected payload type for {path}: {type(payload)}")
            return payload
        except Exception as exc:  # requests exceptions + validation
            last_exc = exc
            if attempt >= max(retries, 1) - 1:
                break
            time.sleep(min(2.0, 0.4 * (2**attempt)))
    assert last_exc is not None
    raise last_exc


def fetch_json(base_url: str, path: str, timeout_seconds: float, retries: int) -> Dict[str, Any]:
    return _request_json_with_retries("GET", base_url, path, timeout_seconds, retries)


def post_json(base_url: str, path: str, timeout_seconds: float, retries: int) -> Dict[str, Any]:
    return _request_json_with_retries("POST", base_url, path, timeout_seconds, retries)


def _delta(end: Dict[str, Any], start: Dict[str, Any], key: str) -> int:
    return int(end.get(key, 0)) - int(start.get(key, 0))


def _rollout_key(telemetry: Dict[str, Any], key: str) -> str:
    rollout_key = f"rollout_{key}"
    if bool(telemetry.get("rollout_scope_enabled", False)) and rollout_key in telemetry:
        return rollout_key
    return key


def build_blockers(
    *,
    final_gates: Dict[str, Any],
    delta: Dict[str, int],
    window_rates: Dict[str, float],
    thresholds: Dict[str, float],
    enforce_tmux_disagreement: bool,
    tmux_comparison_enabled: bool,
    parser_lines_delta: int,
    sample_count: int,
) -> List[Dict[str, str]]:
    blockers: List[Dict[str, str]] = []
    gates = final_gates.get("gates", {})

    disagreement = float(window_rates["jsonl_vs_tmux_disagreement_rate"])
    fallback = float(window_rates["fallback_trigger_rate"])
    watcher_error = float(window_rates["watcher_error_rate"])
    unmapped = int(
        gates.get("unmapped_rollout_terminals", {}).get(
            "value", gates.get("unmapped_active_terminals", {}).get("value", 0)
        )
    )

    if tmux_comparison_enabled and delta["jsonl_tmux_comparisons"] < int(thresholds["min_comparisons"]):
        blockers.append(
            {
                "blocker": "insufficient_status_comparisons",
                "why": "Canary did not observe enough status comparisons to trust disagreement rate.",
                "action": "Run a longer/busier canary before promoting JSONL-only.",
            }
        )
    if sample_count <= 0:
        blockers.append(
            {
                "blocker": "insufficient_snapshot_samples",
                "why": "No successful telemetry snapshots were collected during the window.",
                "action": "Increase timeout/retries and rerun the window under stable CAO API load.",
            }
        )
    observed_watcher_events = max(
        int(delta["trigger_jsonl_events"]),
        int(delta.get("jsonl_watcher_events_received", 0)),
    )
    structured_jsonl_activity = bool(
        int(delta.get("jsonl_status_checks", 0)) > 0 and parser_lines_delta > 0
    )
    if (
        observed_watcher_events < int(thresholds["min_jsonl_events"])
        and not structured_jsonl_activity
    ):
        blockers.append(
            {
                "blocker": "insufficient_jsonl_events",
                "why": "Watcher event volume is too low to prove steady-state behavior.",
                "action": "Drive real agent activity; verify JSONL watcher event volume increases or parser line progress is observed.",
            }
        )
    min_deliveries = int(thresholds["min_deliveries"])
    if delta["deliveries_succeeded"] < min_deliveries:
        if int(delta.get("delivery_attempts", 0)) == 0:
            blockers.append(
                {
                    "blocker": "insufficient_delivery_attempts",
                    "why": "No delivery attempts were observed; ready-state delivery path was not exercised.",
                    "action": "Enable verifier delivery probe or generate pending messages while terminal reaches ready state.",
                }
            )
        else:
            blockers.append(
                {
                    "blocker": "no_successful_deliveries",
                    "why": "No successful inbox delivery happened during the verification window.",
                    "action": "Run controlled pending-message tests while terminal transitions from waiting to ready.",
                }
            )
    if enforce_tmux_disagreement and disagreement >= thresholds["max_disagreement_rate"]:
        blockers.append(
            {
                "blocker": "disagreement_rate_too_high",
                "why": f"JSONL vs tmux disagreement rate is {disagreement:.4f}.",
                "action": "Inspect `last_disagreements`, classify transient vs persistent, and fix parser/state mapping gaps.",
            }
        )
    if fallback >= thresholds["max_fallback_rate"]:
        blockers.append(
            {
                "blocker": "fallback_rate_too_high",
                "why": f"Fallback rate is {fallback:.4f}, indicating mapping/resolution instability.",
                "action": "Fix deterministic session mapping and stale mapping invalidation before promotion.",
            }
        )
    if watcher_error >= thresholds["max_watcher_error_rate"]:
        blockers.append(
            {
                "blocker": "watcher_error_rate_too_high",
                "why": f"Watcher error rate is {watcher_error:.4f}.",
                "action": "Fix watcher exceptions and retry canary.",
            }
        )
    if unmapped > 0:
        blockers.append(
            {
                "blocker": "unmapped_rollout_terminals",
                "why": f"{unmapped} rollout terminal(s) still have non-deterministic or missing mapping.",
                "action": "Backfill metadata and enforce deterministic provider session IDs.",
            }
        )
    return blockers


def _resolve_probe_terminal_id(
    *,
    base_url: str,
    timeout_seconds: float,
    retries: int,
    terminal_id: str,
    session_name: str,
    auto_session_name: str,
    create_session: bool,
    provider: str,
    agent_profile: str,
    working_directory: str,
) -> str:
    if terminal_id:
        return terminal_id

    candidate_session_names: List[str] = []
    for name in (session_name, auto_session_name):
        if name and name not in candidate_session_names:
            candidate_session_names.append(name)

    default_working_directory = working_directory or _default_probe_working_directory()

    for name in candidate_session_names:
        for attempt in range(max(retries, 1)):
            try:
                response = requests.get(
                    f"{base_url.rstrip('/')}/sessions/{name}",
                    timeout=timeout_seconds,
                )
                if response.status_code == 404 and create_session:
                    created = requests.post(
                        f"{base_url.rstrip('/')}/sessions",
                        params={
                            "provider": provider,
                            "agent_profile": agent_profile,
                            "session_name": name,
                            "working_directory": default_working_directory,
                        },
                        timeout=timeout_seconds,
                    )
                    created.raise_for_status()
                    created_payload = created.json()
                    if isinstance(created_payload, dict) and created_payload.get("id"):
                        return str(created_payload["id"])
                    break

                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    break
                terminals = payload.get("terminals") or []
                if isinstance(terminals, list):
                    for item in terminals:
                        if isinstance(item, dict) and item.get("id"):
                            return str(item["id"])
                break
            except Exception:
                if attempt >= max(retries, 1) - 1:
                    break
                time.sleep(0.3)

    try:
        telemetry = fetch_json(base_url, "/diagnostics/inbox/telemetry", timeout_seconds, retries)
        rollout_terminal_ids = telemetry.get("rollout_scope_terminal_ids") or []
        if isinstance(rollout_terminal_ids, list):
            for item in rollout_terminal_ids:
                if item:
                    return str(item)
    except Exception:
        pass

    for _ in range(max(retries, 1)):
        try:
            response = requests.get(
                f"{base_url.rstrip('/')}/sessions",
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                break
            for session in payload:
                if not isinstance(session, dict):
                    continue
                session_id = session.get("id") or session.get("name")
                if not session_id:
                    continue
                try:
                    session_payload = fetch_json(
                        base_url,
                        f"/sessions/{session_id}",
                        timeout_seconds,
                        retries,
                    )
                except Exception:
                    continue
                terminals = session_payload.get("terminals") or []
                if isinstance(terminals, list):
                    for item in terminals:
                        if isinstance(item, dict) and item.get("id"):
                            return str(item["id"])
            break
        except Exception:
            time.sleep(0.3)
    return ""


def _start_delivery_probe(
    *,
    base_url: str,
    timeout_seconds: float,
    retries: int,
    terminal_id: str,
    message_interval_seconds: float,
    prompt_interval_seconds: float,
    send_prompts: bool,
) -> tuple[threading.Event, threading.Thread]:
    stop_event = threading.Event()

    def _loop() -> None:
        msg_counter = 0
        prompt_counter = 0
        next_msg_at = time.monotonic()
        next_prompt_at = time.monotonic()

        while not stop_event.is_set():
            now = time.monotonic()
            try:
                if now >= next_msg_at:
                    msg_counter += 1
                    sender = f"verifier-{uuid.uuid4().hex[:8]}"
                    message = f"verifier_probe_msg_{msg_counter}"
                    # Fire-and-forget probe; failures are tolerated in canary exercise mode.
                    requests.post(
                        (
                            f"{base_url.rstrip('/')}/terminals/{terminal_id}/inbox/messages"
                            f"?sender_id={sender}&message={message}"
                        ),
                        timeout=timeout_seconds,
                    )
                    next_msg_at = now + max(message_interval_seconds, 0.2)

                if send_prompts and now >= next_prompt_at:
                    prompt_counter += 1
                    prompt = f"Reply with exactly VERIFIER_PROBE_{prompt_counter} and then stop."
                    requests.post(
                        f"{base_url.rstrip('/')}/terminals/{terminal_id}/input",
                        params={"message": prompt},
                        timeout=timeout_seconds,
                    )
                    next_prompt_at = now + max(prompt_interval_seconds, 2.0)
            except Exception:
                pass

            stop_event.wait(0.25)

    thread = threading.Thread(target=_loop, name="verify-jsonl-delivery-probe", daemon=True)
    thread.start()
    return stop_event, thread


def main() -> int:
    args = parse_args()
    thresholds = {
        "min_comparisons": args.min_comparisons,
        "min_jsonl_events": args.min_jsonl_events,
        "min_deliveries": args.min_deliveries,
        "max_disagreement_rate": args.max_disagreement_rate,
        "max_fallback_rate": args.max_fallback_rate,
        "max_watcher_error_rate": args.max_watcher_error_rate,
    }

    remediation_result: Dict[str, Any] | None = None
    if not args.skip_remediation_before_window:
        remediation_script = Path(__file__).with_name("remediate_jsonl_blockers.py")
        if remediation_script.exists():
            try:
                completed = subprocess.run(
                    [sys.executable, str(remediation_script)],
                    capture_output=True,
                    text=True,
                    timeout=max(args.remediation_timeout_seconds, 1.0),
                    check=False,
                )
                parsed_stdout: Dict[str, Any] | None = None
                stdout = (completed.stdout or "").strip()
                if stdout:
                    try:
                        loaded = json.loads(stdout)
                        if isinstance(loaded, dict):
                            parsed_stdout = loaded
                    except Exception:
                        parsed_stdout = None
                remediation_result = {
                    "script": str(remediation_script),
                    "exit_code": completed.returncode,
                    "timed_out": False,
                    "stdout_json": parsed_stdout,
                    "stderr_tail": (completed.stderr or "")[-2000:] or None,
                }
            except subprocess.TimeoutExpired as exc:
                remediation_result = {
                    "script": str(remediation_script),
                    "exit_code": None,
                    "timed_out": True,
                    "stdout_json": None,
                    "stderr_tail": (exc.stderr or "")[-2000:] if exc.stderr else None,
                }
        else:
            remediation_result = {
                "script": str(remediation_script),
                "exit_code": None,
                "timed_out": False,
                "stdout_json": None,
                "stderr_tail": "remediation script not found",
            }

    reset_result: Dict[str, Any] | None = None
    if args.reset_before_window:
        reset_result = post_json(
            args.base_url,
            "/diagnostics/jsonl/reset?reset_parser=true",
            args.timeout_seconds,
            args.request_retries,
        )

    started_at = datetime.now(timezone.utc)
    first_telemetry = fetch_json(
        args.base_url, "/diagnostics/inbox/telemetry", args.timeout_seconds, args.request_retries
    )
    snapshots: List[Dict[str, Any]] = []
    snapshot_errors: List[Dict[str, Any]] = []
    consecutive_snapshot_errors = 0
    probe_terminal_id = ""
    probe_stop_event: threading.Event | None = None
    probe_thread: threading.Thread | None = None
    probe_enabled = bool(args.delivery_probe)
    if args.delivery_probe_mode == "on":
        probe_enabled = True
    elif args.delivery_probe_mode == "off":
        probe_enabled = False
    elif args.delivery_probe_mode == "auto":
        probe_enabled = int(args.min_deliveries) > 0

    if probe_enabled:
        probe_create_session = bool(args.delivery_probe_create_session)
        probe_terminal_id = _resolve_probe_terminal_id(
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
            retries=args.request_retries,
            terminal_id=args.delivery_probe_terminal_id,
            session_name=args.delivery_probe_session_name,
            auto_session_name=args.delivery_probe_auto_session_name,
            create_session=probe_create_session,
            provider=args.delivery_probe_provider,
            agent_profile=args.delivery_probe_agent_profile,
            working_directory=args.delivery_probe_working_directory,
        )
        if probe_terminal_id:
            probe_stop_event, probe_thread = _start_delivery_probe(
                base_url=args.base_url,
                timeout_seconds=args.timeout_seconds,
                retries=args.request_retries,
                terminal_id=probe_terminal_id,
                message_interval_seconds=args.delivery_probe_message_interval_seconds,
                prompt_interval_seconds=args.delivery_probe_prompt_interval_seconds,
                send_prompts=not args.delivery_probe_disable_prompts,
            )

    deadline = time.time() + max(args.duration_seconds, 0)
    try:
        while True:
            captured_at = datetime.now(timezone.utc).isoformat()
            try:
                telemetry = fetch_json(
                    args.base_url,
                    "/diagnostics/inbox/telemetry",
                    args.timeout_seconds,
                    args.request_retries,
                )
                gates = fetch_json(
                    args.base_url,
                    "/diagnostics/jsonl/gates",
                    args.timeout_seconds,
                    args.request_retries,
                )
                snapshots.append(
                    {
                        "captured_at": captured_at,
                        "telemetry": telemetry,
                        "gates": gates,
                    }
                )
                consecutive_snapshot_errors = 0
            except Exception as exc:
                consecutive_snapshot_errors += 1
                snapshot_errors.append(
                    {
                        "captured_at": captured_at,
                        "error": str(exc),
                        "consecutive_errors": consecutive_snapshot_errors,
                    }
                )
                if consecutive_snapshot_errors == 1 or consecutive_snapshot_errors % 5 == 0:
                    print(
                        json.dumps(
                            {
                                "warning": "snapshot_fetch_failed",
                                "captured_at": captured_at,
                                "consecutive_errors": consecutive_snapshot_errors,
                                "error": str(exc),
                            }
                        )
                    )

            if time.time() >= deadline:
                break
            time.sleep(max(args.interval_seconds, 0.1))
    finally:
        if probe_stop_event is not None:
            probe_stop_event.set()
        if probe_thread is not None:
            probe_thread.join(timeout=3.0)

    if snapshots:
        last_telemetry = snapshots[-1]["telemetry"]
        final_gates = snapshots[-1]["gates"]
        first_gates = snapshots[0]["gates"]
    else:
        last_telemetry = first_telemetry
        try:
            final_gates = fetch_json(
                args.base_url, "/diagnostics/jsonl/gates", args.timeout_seconds, args.request_retries
            )
        except Exception as exc:
            snapshot_errors.append(
                {
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "error": f"final_gates_fetch_failed: {exc}",
                    "consecutive_errors": consecutive_snapshot_errors + 1,
                }
            )
            final_gates = {"gates": {}, "overall_pass": False}
        first_gates = final_gates

    trigger_log_events_key = _rollout_key(first_telemetry, "trigger_log_events")
    trigger_jsonl_events_key = _rollout_key(first_telemetry, "trigger_jsonl_events")
    trigger_poll_events_key = _rollout_key(first_telemetry, "trigger_poll_events")
    jsonl_status_checks_key = _rollout_key(first_telemetry, "jsonl_status_checks")
    jsonl_fallback_checks_key = _rollout_key(first_telemetry, "jsonl_fallback_checks")
    jsonl_tmux_comparisons_key = _rollout_key(first_telemetry, "jsonl_tmux_comparisons")
    jsonl_tmux_disagreements_key = _rollout_key(first_telemetry, "jsonl_tmux_disagreements")
    deliveries_succeeded_key = _rollout_key(first_telemetry, "deliveries_succeeded")
    deliveries_failed_key = _rollout_key(first_telemetry, "deliveries_failed")
    delivery_attempts_key = _rollout_key(first_telemetry, "delivery_attempts")
    watcher_errors_key = _rollout_key(first_telemetry, "watcher_errors")
    watcher_events_received_key = _rollout_key(first_telemetry, "jsonl_watcher_events_received")
    tmux_comparison_enabled = bool(first_telemetry.get("jsonl_tmux_comparison_enabled", True))

    delta = {
        "trigger_log_events": _delta(last_telemetry, first_telemetry, trigger_log_events_key),
        "trigger_jsonl_events": _delta(last_telemetry, first_telemetry, trigger_jsonl_events_key),
        "trigger_poll_events": _delta(last_telemetry, first_telemetry, trigger_poll_events_key),
        "status_checks_total": _delta(last_telemetry, first_telemetry, "status_checks_total"),
        "jsonl_status_checks": _delta(last_telemetry, first_telemetry, jsonl_status_checks_key),
        "jsonl_fallback_checks": _delta(
            last_telemetry, first_telemetry, jsonl_fallback_checks_key
        ),
        "jsonl_tmux_comparisons": _delta(
            last_telemetry, first_telemetry, jsonl_tmux_comparisons_key
        ),
        "jsonl_tmux_disagreements": _delta(
            last_telemetry, first_telemetry, jsonl_tmux_disagreements_key
        ),
        "deliveries_succeeded": _delta(last_telemetry, first_telemetry, deliveries_succeeded_key),
        "deliveries_failed": _delta(last_telemetry, first_telemetry, deliveries_failed_key),
        "delivery_attempts": _delta(last_telemetry, first_telemetry, delivery_attempts_key),
        "jsonl_watcher_events_received": _delta(
            last_telemetry, first_telemetry, watcher_events_received_key
        ),
        "watcher_errors": _delta(last_telemetry, first_telemetry, watcher_errors_key),
    }

    events_delta = (
        delta["trigger_log_events"] + delta["trigger_jsonl_events"] + delta["trigger_poll_events"]
    )
    window_rates = {
        "jsonl_vs_tmux_disagreement_rate": (
            delta["jsonl_tmux_disagreements"] / delta["jsonl_tmux_comparisons"]
            if delta["jsonl_tmux_comparisons"] > 0
            else 0.0
        ),
        "fallback_trigger_rate": (
            delta["jsonl_fallback_checks"] / delta["jsonl_status_checks"]
            if delta["jsonl_status_checks"] > 0
            else 0.0
        ),
        "watcher_error_rate": (
            delta["watcher_errors"] / events_delta if events_delta > 0 else 0.0
        ),
    }
    parser_lines_delta = (
        int(final_gates.get("parser_metrics", {}).get("lines_seen", 0))
        - int(first_gates.get("parser_metrics", {}).get("lines_seen", 0))
    )

    blockers = build_blockers(
        final_gates=final_gates,
        delta=delta,
        window_rates=window_rates,
        thresholds=thresholds,
        enforce_tmux_disagreement=bool(args.enforce_tmux_disagreement),
        tmux_comparison_enabled=tmux_comparison_enabled,
        parser_lines_delta=parser_lines_delta,
        sample_count=len(snapshots),
    )
    recommendation = "promote_jsonl_only" if not blockers else "stay_hybrid"

    report = {
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "base_url": args.base_url,
        "duration_seconds": args.duration_seconds,
        "interval_seconds": args.interval_seconds,
        "thresholds": thresholds,
        "enforce_tmux_disagreement": bool(args.enforce_tmux_disagreement),
        "reset_before_window": bool(args.reset_before_window),
        "remediation_before_window": remediation_result,
        "reset_result": reset_result,
        "delta": delta,
        "window_rates": window_rates,
        "parser_lines_delta": parser_lines_delta,
        "counter_scope_keys": {
            "trigger_log_events": trigger_log_events_key,
            "trigger_jsonl_events": trigger_jsonl_events_key,
            "trigger_poll_events": trigger_poll_events_key,
            "jsonl_status_checks": jsonl_status_checks_key,
            "jsonl_fallback_checks": jsonl_fallback_checks_key,
            "jsonl_tmux_comparisons": jsonl_tmux_comparisons_key,
            "jsonl_tmux_disagreements": jsonl_tmux_disagreements_key,
            "deliveries_succeeded": deliveries_succeeded_key,
            "deliveries_failed": deliveries_failed_key,
            "delivery_attempts": delivery_attempts_key,
            "jsonl_watcher_events_received": watcher_events_received_key,
            "watcher_errors": watcher_errors_key,
        },
        "tmux_comparison_enabled": tmux_comparison_enabled,
        "delivery_probe": {
            "enabled": probe_enabled,
            "mode": args.delivery_probe_mode,
            "terminal_id": probe_terminal_id or None,
            "session_name": args.delivery_probe_session_name or None,
            "auto_session_name": args.delivery_probe_auto_session_name or None,
            "create_session": bool(
                args.delivery_probe_create_session
            ),
            "provider": args.delivery_probe_provider,
            "agent_profile": args.delivery_probe_agent_profile,
            "working_directory": (
                args.delivery_probe_working_directory or _default_probe_working_directory()
            ),
            "message_interval_seconds": args.delivery_probe_message_interval_seconds,
            "prompt_interval_seconds": args.delivery_probe_prompt_interval_seconds,
            "disable_prompts": bool(args.delivery_probe_disable_prompts),
        },
        "final_gates": final_gates,
        "recommendation": recommendation,
        "blockers": blockers,
        "sample_count": len(snapshots),
        "snapshot_error_count": len(snapshot_errors),
        "snapshot_errors_tail": snapshot_errors[-20:],
    }

    out_path = Path(args.out_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Wrote report: {out_path}")
    return 0 if recommendation == "promote_jsonl_only" else 1


if __name__ == "__main__":
    raise SystemExit(main())
