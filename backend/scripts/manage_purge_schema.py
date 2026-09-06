"""显式 plan/check/apply 永久清除 additive schema；绝不随应用启动运行。"""

from __future__ import annotations

import argparse
import json
import sys

from services.purge_schema_manager import (
    PostgresPurgeSchemaSnapshotReader,
    PurgeSchemaManager,
)


def create_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("plan")
    subcommands.add_parser("check")
    apply = subcommands.add_parser("apply")
    apply.add_argument("--expected-plan-sha256", required=True)
    apply.add_argument("--acknowledge-additive", action="store_true")
    return parser


def main(
    argv=None,
    *,
    connection_factory=None,
    stdout=sys.stdout,
    stderr=sys.stderr,
):
    args = create_parser().parse_args(argv)
    manager = PurgeSchemaManager()
    plan = manager.plan()
    if args.command == "plan":
        _write(stdout, {
            "status": "planned",
            "migration": plan.migration,
            "statement_count": plan.statement_count,
            "plan_sha256": plan.sha256,
        })
        return 0
    if args.command == "apply" and not args.acknowledge_additive:
        _write(stderr, {
            "status": "rejected",
            "error_code": "PURGE_SCHEMA_APPLY_NOT_ACKNOWLEDGED",
        })
        return 2
    if connection_factory is not None:
        return _with_connection(
            connection_factory(), args, manager, stdout, stderr,
        )
    return _with_application_connection(args, manager, stdout, stderr)


def _with_application_connection(args, manager, stdout, stderr):
    from app import create_app
    from models import db

    app = create_app()
    with app.app_context():
        if args.command == "apply":
            with db.engine.begin() as connection:
                return _with_connection(
                    connection, args, manager, stdout, stderr,
                )
        with db.engine.connect() as connection:
            return _with_connection(connection, args, manager, stdout, stderr)


def _with_connection(connection, args, manager, stdout, stderr):
    try:
        if args.command == "check":
            check = manager.check(PostgresPurgeSchemaSnapshotReader(connection).read())
            _write(stdout, {
                "status": "ready" if check.ready else "not_ready",
                "ready": check.ready,
                "missing": list(check.missing),
                "plan_sha256": check.plan_sha256,
            })
            return 0 if check.ready else 3
        plan = manager.apply(
            connection,
            expected_plan_sha256=args.expected_plan_sha256,
            acknowledge_additive=args.acknowledge_additive,
        )
        _write(stdout, {
            "status": "applied",
            "migration": plan.migration,
            "statement_count": plan.statement_count,
            "plan_sha256": plan.sha256,
        })
        return 0
    except ValueError as exc:
        _write(stderr, {
            "status": "rejected",
            "error_code": "PURGE_SCHEMA_PLAN_MISMATCH",
            "error": str(exc),
        })
        return 2
    except Exception as exc:
        _write(stderr, {
            "status": "failed",
            "error_code": "PURGE_SCHEMA_OPERATION_FAILED",
            "error": type(exc).__name__,
        })
        return 4


def _write(stream, payload):
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
