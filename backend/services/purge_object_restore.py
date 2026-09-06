"""永久清除对象备份的只读复验与仅隔离位置恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional, Protocol

from .backup_storage import (
    BackupObject,
    BackupStorageConflictError,
    BackupStorageError,
)
from .purge_object_backup import PurgeObjectBackupManifest


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PurgeObjectRestoreError(RuntimeError):
    def __init__(self, message: str, *, stage: str, error_code: str):
        super().__init__(message)
        self.stage = stage
        self.error_code = error_code


class PurgeObjectRestoreConfigError(PurgeObjectRestoreError):
    def __init__(self, message: str, *, error_code: str = "invalid_config"):
        super().__init__(message, stage="config", error_code=error_code)


class PurgeObjectRestoreIntegrityError(PurgeObjectRestoreError):
    def __init__(
        self,
        message: str,
        *,
        stage: str = "restore_verify",
        error_code: str = "restore_integrity_failed",
    ):
        super().__init__(message, stage=stage, error_code=error_code)


class PurgeObjectRestoreConflictError(PurgeObjectRestoreIntegrityError):
    def __init__(self, message: str, *, stage: str = "isolation_reconcile"):
        super().__init__(
            message,
            stage=stage,
            error_code="isolated_restore_conflict",
        )


class ReadableObjectStorage(Protocol):
    def head(self, key: str) -> Optional[BackupObject]:
        ...

    def download_to(self, key: str, target: BinaryIO) -> None:
        ...


class FileWriteOnceObjectStorage(ReadableObjectStorage, Protocol):
    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        ...


@dataclass(frozen=True)
class PurgeObjectRestoreConfig:
    formal_bucket: str
    backup_bucket: str
    backup_prefix: str
    isolated_bucket: str
    isolated_prefix: str
    isolated_environment: bool
    temporary_root: Path

    def __post_init__(self) -> None:
        buckets = {
            self.formal_bucket,
            self.backup_bucket,
            self.isolated_bucket,
        }
        if any(not _is_safe_bucket(bucket) for bucket in buckets):
            raise PurgeObjectRestoreConfigError("对象存储 Bucket 身份无效")
        if len(buckets) != 3:
            raise PurgeObjectRestoreConfigError(
                "隔离恢复 Bucket 必须与正式及备份 Bucket 不同",
                error_code="target_not_isolated",
            )
        if not _is_safe_key(self.backup_prefix) or not _is_safe_key(
            self.isolated_prefix
        ):
            raise PurgeObjectRestoreConfigError("对象存储前缀不安全")


@dataclass(frozen=True)
class ObjectCopyVerification:
    status: str
    manifest_sha256: str
    object_count: int

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "status": self.status,
            "manifest_sha256": self.manifest_sha256,
            "object_count": self.object_count,
        }


@dataclass(frozen=True)
class RestoredObjectResult:
    object_id: str
    kind: str
    formal_key: str
    backup_key: str
    isolated_key: str
    size: int
    sha256: str
    verification: str

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "object_id": self.object_id,
            "kind": self.kind,
            "formal_key": self.formal_key,
            "backup_key": self.backup_key,
            "isolated_key": self.isolated_key,
            "size_bytes": self.size,
            "sha256": self.sha256,
            "verification": self.verification,
        }


@dataclass(frozen=True)
class IsolatedObjectRestoreResult:
    status: str
    purge_batch_id: str
    restore_run_id: str
    isolated_bucket: str
    objects: tuple[RestoredObjectResult, ...]

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "status": self.status,
            "purge_batch_id": self.purge_batch_id,
            "restore_run_id": self.restore_run_id,
            "isolated_bucket": self.isolated_bucket,
            "objects": [item.to_dict() for item in self.objects],
        }


class PurgeObjectRestoreService:
    """使用独立读/写端口复验副本，并且只恢复到隔离 Bucket。"""

    def __init__(
        self,
        *,
        backup_store: ReadableObjectStorage,
        isolated_store: FileWriteOnceObjectStorage,
        config: PurgeObjectRestoreConfig,
    ):
        self.backup_store = backup_store
        self.isolated_store = isolated_store
        self.config = config

    def verify_copies(
        self,
        manifest: PurgeObjectBackupManifest,
    ) -> ObjectCopyVerification:
        manifest_bytes = self._validated_manifest_bytes(manifest)
        self._verify_object(
            self.backup_store,
            manifest.manifest_key,
            expected_size=len(manifest_bytes),
            expected_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            expected_metadata={
                "purge-batch-id": manifest.purge_batch_id,
                "kind": "purge-object-manifest",
                "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            },
            stage="manifest_readback",
            conflict=False,
        )
        remote_manifest = _download_bytes(
            self.backup_store,
            manifest.manifest_key,
            maximum_size=1024 * 1024,
        )
        if remote_manifest != manifest_bytes:
            raise PurgeObjectRestoreIntegrityError(
                "远端对象备份 manifest 内容不匹配",
                stage="manifest_readback",
                error_code="invalid_manifest",
            )
        try:
            parsed = PurgeObjectBackupManifest.from_dict(
                json.loads(remote_manifest)
            )
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PurgeObjectRestoreIntegrityError(
                "远端对象备份 manifest 无效",
                stage="manifest_readback",
                error_code="invalid_manifest",
            ) from exc
        if _json_bytes(parsed.to_dict()) != remote_manifest:
            raise PurgeObjectRestoreIntegrityError(
                "远端对象备份 manifest 不是 canonical JSON",
                stage="manifest_readback",
                error_code="invalid_manifest",
            )

        for item in manifest.objects:
            self._verify_object(
                self.backup_store,
                item.backup_key,
                expected_size=item.size,
                expected_sha256=item.sha256,
                expected_metadata={
                    "purge-batch-id": manifest.purge_batch_id,
                    "object-id": item.object_id,
                    "object-kind": item.kind,
                    "sha256": item.sha256,
                    "retention-days": "30",
                },
                stage="backup_copy_verify",
                conflict=False,
            )
        return ObjectCopyVerification(
            status="verified",
            manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
            object_count=len(manifest.objects),
        )

    def restore_to_isolation(
        self,
        manifest: PurgeObjectBackupManifest,
        *,
        restore_run_id: str,
        acknowledge_isolated: bool,
    ) -> IsolatedObjectRestoreResult:
        if not acknowledge_isolated:
            raise PurgeObjectRestoreConfigError(
                "隔离恢复必须显式确认",
                error_code="isolation_ack_required",
            )
        if not self.config.isolated_environment:
            raise PurgeObjectRestoreConfigError(
                "恢复目标未声明为一次性隔离环境",
                error_code="target_not_isolated",
            )
        if not _SAFE_IDENTIFIER.fullmatch(restore_run_id):
            raise PurgeObjectRestoreConfigError(
                "隔离恢复运行标识包含不安全字符",
                error_code="invalid_restore_run_id",
            )
        self.verify_copies(manifest)

        temporary_root = Path(self.config.temporary_root)
        temporary_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(temporary_root, 0o700)
        restored: list[RestoredObjectResult] = []
        with tempfile.TemporaryDirectory(
            prefix="purge-object-restore-",
            dir=temporary_root,
        ) as temporary_name:
            temporary_directory = Path(temporary_name)
            os.chmod(temporary_directory, 0o700)
            for item in manifest.objects:
                path = temporary_directory / item.object_id
                with path.open("xb") as target:
                    os.chmod(path, 0o600)
                    self.backup_store.download_to(item.backup_key, target)
                    target.flush()
                    os.fsync(target.fileno())
                size, sha256 = _hash_file(path)
                if size != item.size or sha256 != item.sha256:
                    raise PurgeObjectRestoreIntegrityError(
                        "隔离恢复前的备份对象字节校验失败",
                        stage="backup_download",
                        error_code="backup_verification_failed",
                    )
                isolated_key = (
                    f"{self.config.isolated_prefix.strip('/')}/"
                    f"{restore_run_id}/"
                    f"{manifest.database_restore_point['backup_id']}/objects/"
                    f"{item.kind}/{item.object_id}"
                )
                metadata = {
                    "purge-batch-id": manifest.purge_batch_id,
                    "restore-run-id": restore_run_id,
                    "object-id": item.object_id,
                    "object-kind": item.kind,
                    "sha256": item.sha256,
                    "isolation-only": "true",
                }
                if self.isolated_store.head(isolated_key) is None:
                    try:
                        self.isolated_store.put_file_if_absent(
                            isolated_key,
                            path,
                            metadata=metadata,
                        )
                    except BackupStorageConflictError:
                        pass
                    except BackupStorageError as exc:
                        raise PurgeObjectRestoreIntegrityError(
                            "隔离目标写入失败",
                            stage="isolation_put",
                            error_code="isolation_storage_failed",
                        ) from exc
                self._verify_object(
                    self.isolated_store,
                    isolated_key,
                    expected_size=item.size,
                    expected_sha256=item.sha256,
                    expected_metadata=metadata,
                    stage="isolation_reconcile",
                    conflict=True,
                )
                restored.append(
                    RestoredObjectResult(
                        object_id=item.object_id,
                        kind=item.kind,
                        formal_key=item.formal_key,
                        backup_key=item.backup_key,
                        isolated_key=isolated_key,
                        size=item.size,
                        sha256=item.sha256,
                        verification="passed",
                    )
                )
        return IsolatedObjectRestoreResult(
            status="verified",
            purge_batch_id=manifest.purge_batch_id,
            restore_run_id=restore_run_id,
            isolated_bucket=self.config.isolated_bucket,
            objects=tuple(restored),
        )

    def _validated_manifest_bytes(
        self,
        manifest: PurgeObjectBackupManifest,
    ) -> bytes:
        # round trip invokes the exact-schema validator even when callers built
        # a dataclass directly instead of loading JSON through from_dict().
        validated = PurgeObjectBackupManifest.from_dict(manifest.to_dict())
        manifest_bytes = _json_bytes(validated.to_dict())
        expected_base = (
            f"{self.config.backup_prefix.strip('/')}/"
            f"purge-{manifest.purge_batch_id}/objects"
        )
        if (
            manifest.manifest_key != f"{expected_base}/manifest.json"
            or manifest.plan_key != f"{expected_base}/plan.json"
            or any(
                item.backup_bucket != self.config.backup_bucket
                or item.formal_bucket != self.config.formal_bucket
                or not item.backup_key.startswith(f"{expected_base}/payloads/")
                for item in manifest.objects
            )
            or any(
                item.formal_bucket != self.config.formal_bucket
                for item in manifest.reference_protected
            )
        ):
            raise PurgeObjectRestoreIntegrityError(
                "对象备份 manifest 与受控存储配置不匹配",
                stage="manifest_validate",
                error_code="invalid_manifest",
            )
        return manifest_bytes

    def _verify_object(
        self,
        storage: ReadableObjectStorage,
        key: str,
        *,
        expected_size: int,
        expected_sha256: str,
        expected_metadata: Mapping[str, str],
        stage: str,
        conflict: bool,
    ) -> None:
        found = storage.head(key)
        normalized = (
            {
                str(name).lower(): str(value)
                for name, value in found.metadata.items()
            }
            if found is not None
            else {}
        )
        if (
            found is None
            or found.size != expected_size
            or any(
                normalized.get(str(name).lower()) != str(value)
                for name, value in expected_metadata.items()
            )
        ):
            error = PurgeObjectRestoreConflictError if conflict else (
                PurgeObjectRestoreIntegrityError
            )
            raise error(
                "对象 HEAD 身份、大小或 metadata 不匹配",
                stage=stage,
            )
        size, sha256 = _download_size_sha256(storage, key)
        if size != expected_size or sha256 != expected_sha256:
            error = PurgeObjectRestoreConflictError if conflict else (
                PurgeObjectRestoreIntegrityError
            )
            raise error("对象下载内容校验失败", stage=stage)


def _download_size_sha256(
    storage: ReadableObjectStorage,
    key: str,
) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with tempfile.TemporaryFile(mode="w+b") as target:
        storage.download_to(key, target)
        target.seek(0)
        while True:
            chunk = target.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _download_bytes(
    storage: ReadableObjectStorage,
    key: str,
    *,
    maximum_size: int,
) -> bytes:
    with tempfile.TemporaryFile(mode="w+b") as target:
        storage.download_to(key, target)
        size = target.tell()
        if size > maximum_size:
            raise PurgeObjectRestoreIntegrityError(
                "对象备份 manifest 超出安全大小限制",
                stage="manifest_readback",
                error_code="invalid_manifest",
            )
        target.seek(0)
        return target.read()


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _is_safe_key(key: str) -> bool:
    return bool(
        isinstance(key, str)
        and key
        and not key.startswith("/")
        and "\\" not in key
        and not any(part in {"", ".", ".."} for part in key.split("/"))
        and not any(ord(character) < 32 for character in key)
    )


def _is_safe_bucket(bucket: str) -> bool:
    return bool(
        isinstance(bucket, str)
        and bucket
        and "/" not in bucket
        and "\\" not in bucket
        and not any(ord(character) < 32 for character in bucket)
    )
