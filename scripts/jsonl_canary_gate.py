#!/usr/bin/env python3
"""Evaluate CAO JSONL migration gates via server diagnostics endpoint."""

from __future__ import annotations

import argparse
import json
import sys

import requests

from cli_agent_orchestrator.constants import API_BASE_URL


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check JSONL canary migration gates")
    parser.add_argument(
        "--base-url",
        default=API_BASE_URL,
        help=f"CAO API base URL (default: {API_BASE_URL})",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=5.0,
        help="HTTP request timeout in seconds",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = f"{args.base_url.rstrip('/')}/diagnostics/jsonl/gates"

    try:
        response = requests.get(url, timeout=args.timeout_seconds)
        response.raise_for_status()
    except Exception as exc:
        print(f"ERROR: failed to query gate endpoint: {exc}", file=sys.stderr)
        return 2

    payload = response.json()
    overall_pass = bool(payload.get("overall_pass"))
    print(json.dumps(payload, indent=2, sort_keys=True))

    if overall_pass:
        print("JSONL canary gates: PASS")
        return 0

    print("JSONL canary gates: FAIL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
