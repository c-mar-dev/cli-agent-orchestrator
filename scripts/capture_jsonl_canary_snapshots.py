#!/usr/bin/env python3
"""Capture periodic JSONL canary telemetry + gate snapshots."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture periodic canary snapshots")
    parser.add_argument("--base-url", default="http://127.0.0.1:9891")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument(
        "--out-dir",
        default="work/canary-snapshots",
        help="Directory for snapshot files",
    )
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    return parser.parse_args()


def fetch_json(url: str, timeout: float) -> dict:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for idx in range(args.iterations):
        now = datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%SZ")
        payload = {
            "captured_at": now.isoformat(),
            "base_url": args.base_url,
            "telemetry": fetch_json(
                f"{args.base_url.rstrip('/')}/diagnostics/inbox/telemetry", args.timeout_seconds
            ),
            "gates": fetch_json(
                f"{args.base_url.rstrip('/')}/diagnostics/jsonl/gates", args.timeout_seconds
            ),
        }

        out_path = out_dir / f"snapshot-{idx + 1:03d}-{stamp}.json"
        out_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(out_path)

        if idx < args.iterations - 1:
            time.sleep(args.interval_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
