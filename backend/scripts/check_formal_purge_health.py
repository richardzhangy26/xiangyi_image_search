"""Read-only formal-purge health probe for container/platform checks."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from services.formal_purge_observability import FileFormalPurgeHealthSource


def create_parser():
    parser = argparse.ArgumentParser(description="检查 formal purge 脱敏健康证据")
    parser.add_argument("--evidence", type=Path, required=True)
    return parser


def main(argv=None, *, now=None, stdout=sys.stdout):
    args = create_parser().parse_args(argv)
    snapshot = FileFormalPurgeHealthSource(args.evidence).evaluate(
        now=now or datetime.now(timezone.utc)
    )
    if not snapshot.available:
        payload = {
            "available": False,
            "error_code": "PURGE_FORMAL_HEALTH_UNAVAILABLE",
        }
        stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 2
    payload = {
        "available": True,
        "result": snapshot.result,
        "checked_at": snapshot.checked_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
        "environment_id": snapshot.environment_id,
        "batch_id": str(snapshot.batch_id),
        "stage": snapshot.stage,
        "error_code": snapshot.error_code,
    }
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
