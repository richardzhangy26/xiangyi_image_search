#!/usr/bin/env python3
"""Kodo → OSS 迁移入口；Issue #5 仅开放只读 preflight/dry-run。"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO

from dotenv import load_dotenv

from services.kodo_source import (
    KodoConfig,
    KodoConfigError,
    KodoS3Source,
    PreflightError,
    ReadOnlyObjectSource,
    run_preflight,
    safe_exception_summary,
)

SourceFactory = Callable[[KodoConfig], ReadOnlyObjectSource]


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Kodo → OSS 图片资产迁移（当前仅支持只读前置检查）。"
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
    parser.add_argument("--prefix", default="", help="只扫描此前缀下的对象。")
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
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    args = create_parser().parse_args(argv)
    mode = "preflight" if args.preflight else "dry-run"

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

    try:
        report = run_preflight(
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
        **report.to_dict(),
    }
    serialized = json.dumps(output, ensure_ascii=False, indent=2)
    stdout.write(f"{serialized}\n")

    if args.report_path:
        report_path = args.report_path.expanduser().resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(f"{serialized}\n", encoding="utf-8")
    return 0


def _write_json(stream: TextIO, payload: Mapping[str, object]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
