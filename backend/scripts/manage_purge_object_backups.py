#!/usr/bin/env python3
"""对象备份副本校验和仅隔离位置恢复入口。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, TextIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from services.backup_storage import (
    BackupStorageConfig,
    BackupStorageConfigError,
    BackupStorageError,
    OssBackupStorage,
)
from services.purge_object_backup import (
    PurgeObjectBackupError,
    PurgeObjectBackupManifest,
)
from services.purge_object_restore import (
    PurgeObjectRestoreConfig,
    PurgeObjectRestoreConfigError,
    PurgeObjectRestoreError,
    PurgeObjectRestoreService,
)
from services.purge_object_storage import (
    OssPurgeIsolationStorage,
    PurgeIsolationStorageConfig,
    PurgeObjectStorageConfigError,
)


EXIT_SUCCESS = 0
EXIT_CONFIG = 2
EXIT_INTEGRITY = 3
EXIT_STORAGE = 4
EXIT_RESTORE = 5


class PurgeObjectCliConfigError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_code: str = "invalid_arguments",
    ):
        super().__init__(message)
        self.stage = "config"
        self.error_code = error_code


class PurgeObjectArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise PurgeObjectCliConfigError("命令参数无效")


def create_parser() -> argparse.ArgumentParser:
    parser = PurgeObjectArgumentParser(
        description="复验永久清除对象备份，或仅恢复到隔离 Bucket。"
    )
    parser.add_argument(
        "--env",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env.backup",
        help="独立 ops 环境文件，默认 backend/.env.backup。",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    verify = subcommands.add_parser(
        "verify-copies",
        help="严格复验 final manifest 和每个对象副本。",
    )
    verify.add_argument("--manifest", type=Path, required=True)
    restore = subcommands.add_parser(
        "restore-isolated",
        help="把已验证对象仅恢复到程序派生的隔离位置。",
    )
    restore.add_argument("--manifest", type=Path, required=True)
    restore.add_argument("--restore-run-id", required=True)
    restore.add_argument("--acknowledge-isolated", action="store_true")
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
    backup_storage_factory: Callable[
        [Mapping[str, str]], object
    ] = OssBackupStorage.from_env,
    isolation_storage_factory: Callable[
        [Mapping[str, str]], object
    ] = OssPurgeIsolationStorage.from_env,
    service_factory: Callable[..., object] = PurgeObjectRestoreService,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    command: Optional[str] = None
    try:
        args = create_parser().parse_args(argv)
        command = args.command
        if environ is None:
            load_dotenv(args.env)
            environment: Mapping[str, str] = os.environ
        else:
            environment = dict(environ)
        manifest = _load_manifest(args.manifest.expanduser().resolve())
        formal_buckets = {
            item.formal_bucket for item in manifest.objects
        } | {
            item.formal_bucket for item in manifest.reference_protected
        }
        if len(formal_buckets) != 1:
            raise PurgeObjectCliConfigError(
                "manifest 正式 Bucket 身份不唯一",
                error_code="invalid_manifest_binding",
            )
        formal_bucket = next(iter(formal_buckets))
        backup_config = BackupStorageConfig.from_env(environment)
        if any(
            item.backup_bucket != backup_config.bucket_name
            for item in manifest.objects
        ):
            raise PurgeObjectCliConfigError(
                "manifest 备份 Bucket 身份不匹配",
                error_code="invalid_manifest_binding",
            )
        backup_store = backup_storage_factory(environment)
        temporary_root = Path(
            environment.get(
                "PURGE_RESTORE_TEMP_ROOT",
                str(
                    Path(environment.get("BACKUP_ROOT", "."))
                    / ".purge-object-restore"
                ),
            )
        ).expanduser().resolve()

        if args.command == "verify-copies":
            service = service_factory(
                backup_store=backup_store,
                isolated_store=_UnavailableIsolationStore(),
                config=PurgeObjectRestoreConfig(
                    formal_bucket=formal_bucket,
                    backup_bucket=backup_config.bucket_name,
                    backup_prefix=backup_config.base_prefix,
                    isolated_bucket="verification-not-used.invalid",
                    isolated_prefix="verification-not-used",
                    isolated_environment=False,
                    temporary_root=temporary_root,
                ),
            )
            return _write_success(stdout, service.verify_copies(manifest).to_dict())

        isolation_config = PurgeIsolationStorageConfig.from_env(environment)
        isolated_store = isolation_storage_factory(environment)
        service = service_factory(
            backup_store=backup_store,
            isolated_store=isolated_store,
            config=PurgeObjectRestoreConfig(
                formal_bucket=formal_bucket,
                backup_bucket=backup_config.bucket_name,
                backup_prefix=backup_config.base_prefix,
                isolated_bucket=isolation_config.bucket_name,
                isolated_prefix=isolation_config.base_prefix,
                isolated_environment=isolation_config.isolated_environment,
                temporary_root=temporary_root,
            ),
        )
        result = service.restore_to_isolation(
            manifest,
            restore_run_id=args.restore_run_id,
            acknowledge_isolated=args.acknowledge_isolated,
        )
        return _write_success(stdout, result.to_dict())
    except (
        PurgeObjectCliConfigError,
        BackupStorageConfigError,
        PurgeObjectStorageConfigError,
        PurgeObjectRestoreConfigError,
    ) as exc:
        _write_error(stderr, exc, "config", "invalid_config")
        return EXIT_CONFIG
    except PurgeObjectRestoreError as exc:
        _write_error(stderr, exc, "restore", "restore_failed")
        return EXIT_RESTORE if command == "restore-isolated" else EXIT_INTEGRITY
    except PurgeObjectBackupError as exc:
        _write_error(stderr, exc, "integrity", "invalid_manifest")
        return EXIT_INTEGRITY
    except BackupStorageError as exc:
        _write_error(stderr, exc, "storage", "storage_failed")
        return EXIT_STORAGE
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
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
    except Exception as exc:  # pragma: no cover - 最后一层脱敏边界
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


class _UnavailableIsolationStore:
    """verify-copies 路径不会装配或接触隔离写凭证。"""


def _load_manifest(path: Path) -> PurgeObjectBackupManifest:
    return PurgeObjectBackupManifest.from_dict(
        json.loads(path.read_text(encoding="utf-8"))
    )


def _write_success(stream: TextIO, payload: Mapping[str, object]) -> int:
    _write_json(stream, payload)
    return EXIT_SUCCESS


def _write_error(
    stream: TextIO,
    error: BaseException,
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
