#!/usr/bin/env python3
"""安全、可重跑的 Kodo → OSS → pgvector 图片资产迁移入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, TextIO

from dotenv import load_dotenv

from services.asset_ingest import ImageAssetIngestService
from services.embedding import MAX_BATCH_SIZE, EmbeddingClient
from services.kodo_config import KodoConfig, KodoConfigError
from services.kodo_migration import (
    MigrationError,
    MigrationOptions,
    load_selection_manifest,
    load_retry_report,
    run_migration,
    validate_oss_write_target,
    write_report_atomic,
)
from services.kodo_source import KodoS3Source
from services.object_source import ReadOnlyObjectSource
from services.object_storage import OssObjectStorage
from services.source_preflight import (
    PreflightError,
    run_preflight,
    safe_exception_summary,
)

SourceFactory = Callable[[KodoConfig], ReadOnlyObjectSource]
DependencyFactory = Callable[[Mapping[str, str]], Any]
TERMINAL_FAILURE_EXAMPLE_LIMIT = 5


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kodo → 私有 OSS → pgvector 图片资产迁移。"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--preflight",
        action="store_true",
        help="分页扫描 Kodo，并 HEAD/GET/解码一张小图片。",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="只读扫描来源；不写 OSS、数据库，不调用 embedding。",
    )
    mode.add_argument(
        "--verify-selection",
        action="store_true",
        help="只读下载清单图片，验证格式、哈希与试迁移覆盖范围。",
    )
    mode.add_argument(
        "--pilot",
        type=int,
        metavar="N",
        help="显式试迁移 N 张图片；不会自动进入全量模式。",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help="显式执行当前筛选范围内的迁移。",
    )
    parser.add_argument("--prefix", default="", help="只扫描此前缀下的对象。")
    parser.add_argument(
        "--limit",
        type=int,
        help="进一步限制本次选择的图片数。",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=MAX_BATCH_SIZE,
        help=f"embedding 批大小，自动限制到 1–{MAX_BATCH_SIZE}。",
    )
    parser.add_argument(
        "--max-sample-mb",
        type=float,
        default=10.0,
        help="preflight 样本图片最大 MiB，默认 10。",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        help="可选：将脱敏 JSON 报告写入本地文件。",
    )
    parser.add_argument(
        "--retry-failed",
        type=Path,
        metavar="PREVIOUS_REPORT",
        help="只选择前一份报告中 status=failed 的来源项。",
    )
    parser.add_argument(
        "--selection-manifest",
        type=Path,
        help="试迁移使用的有序来源相对路径 JSON 清单。",
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env",
        help="环境变量文件路径，默认 backend/.env。",
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    source_factory: SourceFactory = KodoS3Source,
    storage_factory: DependencyFactory = (
        lambda environment: OssObjectStorage.from_env(environment)
    ),
    embedding_factory: DependencyFactory = (
        lambda environment: EmbeddingClient(
            api_key=environment.get("DASHSCOPE_API_KEY")
        )
    ),
    app=None,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = create_parser().parse_args(argv)
    mode = (
        "preflight"
        if args.preflight
        else "verify-selection"
        if args.verify_selection
        else "pilot"
        if args.pilot is not None
        else "full"
        if args.full
        else "dry-run"
    )

    if args.max_sample_mb <= 0:
        _write_json(
            stderr,
            {
                "status": "failed",
                "stage": "config",
                "error": "--max-sample-mb 必须大于 0",
            },
        )
        return 2

    if environ is None:
        load_dotenv(args.env)
        environment: Mapping[str, str] = os.environ
    else:
        environment = environ

    try:
        config = KodoConfig.from_env(environment)
    except KodoConfigError as exc:
        _write_json(
            stderr,
            {
                "status": "failed",
                "stage": "config",
                "error": str(exc),
            },
        )
        return 2

    try:
        selection_keys = (
            load_selection_manifest(
                args.selection_manifest.expanduser().resolve()
            )
            if args.selection_manifest
            else ()
        )
        if selection_keys and mode not in {
            "dry-run",
            "pilot",
            "verify-selection",
        }:
            raise MigrationError(
                "selection_manifest",
                "--selection-manifest 仅可用于 dry-run、verify-selection 或 pilot 模式",
            )
        if selection_keys and args.retry_failed:
            raise MigrationError(
                "selection_manifest",
                "--selection-manifest 不能与 --retry-failed 同时使用",
            )
        if selection_keys and mode == "pilot" and args.pilot != len(
            selection_keys
        ):
            raise MigrationError(
                "selection_manifest",
                "--pilot 数量必须与 --selection-manifest 项数一致",
            )
    except MigrationError as exc:
        _write_json(stderr, exc.to_dict())
        return 2

    if mode != "preflight":
        try:
            retry_report = (
                load_retry_report(args.retry_failed.expanduser().resolve())
                if args.retry_failed
                else None
            )
            options = MigrationOptions.build(
                mode=mode,
                prefix=args.prefix,
                pilot_count=args.pilot,
                limit=args.limit,
                batch_size=args.batch_size,
                selection_keys=selection_keys,
                retry_enabled=args.retry_failed is not None,
                retry_failed_keys=(
                    retry_report.failed_keys if retry_report else ()
                ),
                retry_binding=retry_report,
            )
        except (MigrationError, ValueError) as exc:
            if isinstance(exc, MigrationError):
                payload = exc.to_dict()
            else:
                payload = {
                    "status": "failed",
                    "stage": "config",
                    "error": str(exc),
                }
            _write_json(stderr, payload)
            return 2

    try:
        source = source_factory(config)
    except Exception as exc:
        _write_json(
            stderr,
            {
                "status": "failed",
                "stage": "create_source",
                "error": safe_exception_summary(exc),
            },
        )
        return 1

    if mode == "preflight":
        try:
            preflight_report = run_preflight(
                source,
                prefix=args.prefix,
                max_sample_bytes=int(args.max_sample_mb * 1024 * 1024),
            )
        except PreflightError as exc:
            _write_json(stderr, exc.to_dict())
            return 1

        output = {
            "status": "ok",
            "mode": mode,
            "read_only": True,
            "compatibility_aliases_used": list(config.aliases_used),
            **preflight_report.to_dict(),
        }
        try:
            _emit_report(
                output,
                report_path=args.report_path,
                stdout=stdout,
            )
        except MigrationError as exc:
            _write_json(stderr, exc.to_dict())
            return 1
        return 0

    def ingest_service_factory(ingest_source):
        storage = storage_factory(environment)
        validated_prefix = validate_oss_write_target(
            storage,
            environment,
        )
        return ImageAssetIngestService(
            source=ingest_source,
            storage=storage,
            embedding_client=embedding_factory(environment),
            oss_image_base_prefix=validated_prefix,
        )

    try:
        if mode in {"pilot", "full"}:
            application = app or _create_application()
            with application.app_context():
                output = run_migration(
                    source,
                    options=options,
                    ingest_service_factory=ingest_service_factory,
                    before_write=_database_readiness_check,
                )
        else:
            output = run_migration(source, options=options)
    except MigrationError as exc:
        _write_json(stderr, exc.to_dict())
        return 1

    output["compatibility_aliases_used"] = list(config.aliases_used)
    try:
        _emit_report(
            output,
            report_path=args.report_path,
            stdout=stdout,
        )
    except MigrationError as exc:
        _write_json(stderr, exc.to_dict())
        return 1
    return 0 if output["status"] == "ok" else 1


def _create_application():
    # 延迟导入，保证默认 dry-run 不初始化 Flask、数据库或任何写端依赖。
    from app import create_app

    return create_app()


def _database_readiness_check() -> None:
    from sqlalchemy import text

    from models import db

    db.session.execute(text("SELECT 1"))


def _emit_report(
    output: Mapping[str, object],
    *,
    report_path: Optional[Path],
    stdout: TextIO,
) -> None:
    if report_path:
        try:
            write_report_atomic(report_path, dict(output))
        except Exception as exc:
            raise MigrationError(
                "write_report",
                safe_exception_summary(exc),
            ) from exc

    terminal_output = dict(output)
    items = terminal_output.pop("items", None)
    if isinstance(items, list):
        failures = [
            item
            for item in items
            if isinstance(item, dict)
            and (
                item.get("status") == "failed"
                or str(item.get("status", "")).endswith("_conflict")
            )
        ]
        terminal_output["failure_examples"] = [
            {
                "source_relative_path": item.get("source_relative_path"),
                "status": item.get("status"),
                "error_stage": item.get("error_stage"),
                "error": item.get("error"),
            }
            for item in failures[:TERMINAL_FAILURE_EXAMPLE_LIMIT]
        ]
        terminal_output["failure_examples_omitted"] = max(
            0,
            len(failures) - TERMINAL_FAILURE_EXAMPLE_LIMIT,
        )
        terminal_output["complete_report_written"] = report_path is not None

    serialized = json.dumps(
        terminal_output,
        ensure_ascii=False,
        indent=2,
    )
    stdout.write(f"{serialized}\n")


def _write_json(stream: TextIO, payload: Mapping[str, object]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
