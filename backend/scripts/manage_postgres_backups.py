#!/usr/bin/env python3
"""显式 PostgreSQL 备份、恢复点和隔离恢复验证入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO

# 支持手册中的直接脚本调用；模块方式运行时不改变既有导入路径。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from services.backup_storage import (
    BackupStorageConfig,
    BackupStorageConfigError,
    BackupStorageError,
    OssBackupStorage,
)
from services.postgres_backup import (
    BackupConfigError,
    BackupIntegrityError,
    BackupManifest,
    BackupRequest,
    PostgresBackupService,
    PostgresConnectionConfig,
    PostgresRestoreVerifier,
    RestoreSafetyError,
    RestoreVerificationConfig,
    RestoreVerificationError,
    SubprocessCommandRunner,
)


EXIT_SUCCESS = 0
EXIT_CONFIG = 2
EXIT_INTEGRITY = 3
EXIT_STORAGE = 4
EXIT_RESTORE = 5


class BackupArgumentParser(argparse.ArgumentParser):
    """将 argparse 的 usage 错误收口为脱敏 JSON 错误。"""

    def error(self, message: str) -> None:
        raise BackupConfigError(
            "命令参数无效",
            error_code="invalid_arguments",
        )


def create_parser() -> argparse.ArgumentParser:
    parser = BackupArgumentParser(
        description="显式创建和验证 PostgreSQL 备份；不会挂到应用启动。"
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env.backup",
        help="独立 ops 环境文件，默认 backend/.env.backup。",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("create-daily", help="创建或 reconcile 当日全量备份。")
    restore_point = subcommands.add_parser(
        "create-restore-point", help="创建清除批次即时恢复点。"
    )
    restore_point.add_argument("--purge-batch-id", required=True)
    verify_copies = subcommands.add_parser(
        "verify-copies", help="重新校验本机与异机副本。"
    )
    verify_copies.add_argument("--manifest", type=Path, required=True)
    verify_restore = subcommands.add_parser(
        "verify-restore", help="从异机副本恢复到程序新建的隔离数据库。"
    )
    verify_restore.add_argument("--manifest", type=Path, required=True)
    verify_restore.add_argument("--acknowledge-isolated", action="store_true")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    runner_factory: Callable[[], object] = SubprocessCommandRunner,
    storage_factory: Callable[[Mapping[str, str]], object] = OssBackupStorage.from_env,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    try:
        args = create_parser().parse_args(argv)
        if environ is None:
            load_dotenv(args.env)
            environment: Mapping[str, str] = os.environ
        else:
            environment = dict(environ)

        storage_config = BackupStorageConfig.from_env(environment)
        storage = storage_factory(environment)
        runner = runner_factory()

        if args.command in {"create-daily", "create-restore-point", "verify-copies"}:
            source = PostgresConnectionConfig.from_env(
                environment,
                prefix="BACKUP_DB_",
            )
            backup_root_value = environment.get("BACKUP_ROOT")
            if not backup_root_value:
                raise BackupConfigError("缺少专用数据库配置: BACKUP_ROOT")
            service = PostgresBackupService(
                runner=runner,
                storage=storage,
                source=source,
                backup_root=Path(backup_root_value).expanduser().resolve(),
                remote_bucket=storage_config.bucket_name,
                remote_prefix=storage_config.base_prefix,
            )

        if args.command == "create-daily":
            request = BackupRequest.daily_from_environment(environment)
            return _write_success(stdout, service.create_backup(request).to_dict())
        if args.command == "create-restore-point":
            request = BackupRequest.restore_point(args.purge_batch_id)
            return _write_success(stdout, service.create_backup(request).to_dict())
        if args.command == "verify-copies":
            result = service.verify_copies(args.manifest.expanduser().resolve())
            return _write_success(stdout, result.to_dict())
        if args.command == "verify-restore":
            manifest = _load_manifest(args.manifest.expanduser().resolve())
            if manifest.remote_bucket != storage_config.bucket_name:
                raise BackupIntegrityError(
                    "manifest 备份 Bucket 身份不匹配",
                    stage="manifest_validate",
                    error_code="remote_bucket_mismatch",
                )
            restore_config = RestoreVerificationConfig.from_env(environment)
            temporary_root = Path(
                environment.get(
                    "RESTORE_VERIFY_TEMP_ROOT",
                    str(Path(environment.get("BACKUP_ROOT", ".")) / ".restore-verify"),
                )
            ).expanduser().resolve()
            verifier = PostgresRestoreVerifier(
                runner=runner,
                storage=storage,
                config=restore_config,
                temporary_root=temporary_root,
                remote_bucket=storage_config.bucket_name,
                remote_prefix=storage_config.base_prefix,
            )
            result = verifier.verify_from_remote(
                manifest,
                acknowledge_isolated=args.acknowledge_isolated,
            )
            if result.status != "verified":
                _write_json(stderr, result.to_dict())
                return EXIT_RESTORE
            return _write_success(stdout, result.to_dict())
        raise BackupConfigError("未知命令")
    except (BackupStorageConfigError, BackupConfigError) as exc:
        _write_error(stderr, exc, fallback_stage="config", fallback_code="invalid_config")
        return EXIT_CONFIG
    except (RestoreSafetyError, RestoreVerificationError) as exc:
        _write_error(stderr, exc, fallback_stage="restore", fallback_code="restore_failed")
        return EXIT_RESTORE
    except BackupIntegrityError as exc:
        _write_error(stderr, exc, fallback_stage="integrity", fallback_code="integrity_failed")
        return EXIT_INTEGRITY
    except BackupStorageError as exc:
        _write_error(stderr, exc, fallback_stage="storage", fallback_code="storage_failed")
        return EXIT_STORAGE
    except (OSError, json.JSONDecodeError) as exc:
        _write_json(
            stderr,
            {
                "status": "failed",
                "stage": "manifest_validate",
                "error_code": "manifest_unreadable",
                "error": f"manifest 不可读: {type(exc).__name__}",
            },
        )
        return EXIT_INTEGRITY
    except Exception as exc:  # pragma: no cover - 最后一层脱敏防线
        _write_json(
            stderr,
            {
                "status": "failed",
                "stage": "internal",
                "error_code": "internal_error",
                "error": f"内部错误: {type(exc).__name__}",
            },
        )
        return EXIT_INTEGRITY


def _load_manifest(path: Path) -> BackupManifest:
    return BackupManifest.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _write_success(stream: TextIO, payload: Mapping[str, object]) -> int:
    _write_json(stream, payload)
    return EXIT_SUCCESS


def _write_error(
    stream: TextIO,
    error: BaseException,
    *,
    fallback_stage: str,
    fallback_code: str,
) -> None:
    _write_json(
        stream,
        {
            "status": "failed",
            "stage": getattr(error, "stage", fallback_stage),
            "error_code": getattr(error, "error_code", fallback_code),
            "error": str(error),
        },
    )


def _write_json(stream: TextIO, payload: Mapping[str, object]) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
