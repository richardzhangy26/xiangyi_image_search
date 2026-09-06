"""永久清除对象备份使用的角色隔离 OSS Adapter。"""

from __future__ import annotations

import re
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Mapping, Optional

from .backup_storage import (
    BackupObject,
    BackupStorageConflictError,
    BackupStorageError,
)


class PurgeObjectStorageConfigError(BackupStorageError):
    """对象备份角色凭证缺失或身份复用。"""


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FormalObjectObservation:
    formal_bucket: str
    formal_key: str
    size: int
    sha256: str
    etag: str
    observed_at: datetime

    def __post_init__(self):
        _validate_bucket(self.formal_bucket)
        _validate_key(self.formal_key)
        if self.size <= 0 or not _SHA256.fullmatch(self.sha256):
            raise ValueError("formal object byte identity invalid")
        if not self.etag or any(ord(character) < 32 for character in self.etag):
            raise ValueError("formal object etag invalid")
        if self.observed_at.tzinfo is None:
            raise ValueError("formal object observation must include timezone")


@dataclass(frozen=True)
class DeletionObservation:
    result: str
    before: FormalObjectObservation
    deleted_at: datetime
    after_missing: bool

    def __post_init__(self):
        if self.result not in {"deleted", "already_absent_after_intent"}:
            raise ValueError("formal deletion result invalid")
        if self.deleted_at.tzinfo is None or self.after_missing is not True:
            raise ValueError("formal deletion postcondition invalid")


@dataclass(frozen=True)
class PurgeSourceStorageConfig:
    access_key_id: str
    access_key_secret: str
    endpoint: str
    bucket_name: str

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
    ) -> "PurgeSourceStorageConfig":
        prefix = "PURGE_SOURCE_OSS_"
        required = (
            f"{prefix}ACCESS_KEY_ID",
            f"{prefix}ACCESS_KEY_SECRET",
            f"{prefix}ENDPOINT",
            f"{prefix}BUCKET_NAME",
        )
        missing = [name for name in required if not environ.get(name)]
        if missing:
            raise PurgeObjectStorageConfigError(
                f"缺少永久清除源对象只读配置: {', '.join(missing)}"
            )
        access_key_id = environ[f"{prefix}ACCESS_KEY_ID"]
        forbidden_access_keys = {
            value
            for value in (
                environ.get("OSS_ACCESS_KEY_ID"),
                environ.get("BACKUP_OSS_ACCESS_KEY_ID"),
                environ.get("PURGE_RESTORE_OSS_ACCESS_KEY_ID"),
            )
            if value
        }
        if access_key_id in forbidden_access_keys:
            raise PurgeObjectStorageConfigError(
                "正式源对象只读凭证必须独立于应用、备份和恢复凭证"
            )
        bucket_name = environ[f"{prefix}BUCKET_NAME"]
        forbidden_buckets = {
            value
            for value in (
                environ.get("BACKUP_OSS_BUCKET_NAME"),
                environ.get("PURGE_RESTORE_OSS_BUCKET_NAME"),
            )
            if value
        }
        if bucket_name in forbidden_buckets:
            raise PurgeObjectStorageConfigError(
                "正式源对象 Bucket 必须与备份和隔离恢复 Bucket 不同"
            )
        _validate_bucket(bucket_name)
        return cls(
            access_key_id=access_key_id,
            access_key_secret=environ[f"{prefix}ACCESS_KEY_SECRET"],
            endpoint=environ[f"{prefix}ENDPOINT"],
            bucket_name=bucket_name,
        )


@dataclass(frozen=True)
class PurgeIsolationStorageConfig:
    access_key_id: str
    access_key_secret: str
    endpoint: str
    bucket_name: str
    base_prefix: str
    server_side_encryption: str
    isolated_environment: bool

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str],
    ) -> "PurgeIsolationStorageConfig":
        prefix = "PURGE_RESTORE_OSS_"
        required = (
            f"{prefix}ACCESS_KEY_ID",
            f"{prefix}ACCESS_KEY_SECRET",
            f"{prefix}ENDPOINT",
            f"{prefix}BUCKET_NAME",
        )
        missing = [name for name in required if not environ.get(name)]
        if missing:
            raise PurgeObjectStorageConfigError(
                f"缺少隔离恢复对象存储配置: {', '.join(missing)}"
            )
        access_key_id = environ[f"{prefix}ACCESS_KEY_ID"]
        forbidden_access_keys = {
            value
            for value in (
                environ.get("OSS_ACCESS_KEY_ID"),
                environ.get("BACKUP_OSS_ACCESS_KEY_ID"),
                environ.get("PURGE_SOURCE_OSS_ACCESS_KEY_ID"),
            )
            if value
        }
        if access_key_id in forbidden_access_keys:
            raise PurgeObjectStorageConfigError(
                "隔离恢复写入凭证必须独立于应用、备份和源读取凭证"
            )
        bucket_name = environ[f"{prefix}BUCKET_NAME"]
        forbidden_buckets = {
            value
            for value in (
                environ.get("OSS_BUCKET_NAME"),
                environ.get("BACKUP_OSS_BUCKET_NAME"),
                environ.get("PURGE_SOURCE_OSS_BUCKET_NAME"),
            )
            if value
        }
        if bucket_name in forbidden_buckets:
            raise PurgeObjectStorageConfigError(
                "隔离恢复 Bucket 必须与正式及备份 Bucket 不同"
            )
        _validate_bucket(bucket_name)
        base_prefix = environ.get(
            "PURGE_RESTORE_OSS_BASE_PREFIX",
            "isolated-restores",
        ).strip("/")
        _validate_key(base_prefix)
        sse = environ.get("PURGE_RESTORE_OSS_SSE", "AES256")
        if sse not in {"AES256", "KMS"}:
            raise PurgeObjectStorageConfigError(
                "PURGE_RESTORE_OSS_SSE 只允许 AES256 或 KMS"
            )
        isolated_value = environ.get("PURGE_RESTORE_ISOLATED", "0")
        if isolated_value not in {"0", "1"}:
            raise PurgeObjectStorageConfigError(
                "PURGE_RESTORE_ISOLATED 只允许 0 或 1"
            )
        return cls(
            access_key_id=access_key_id,
            access_key_secret=environ[f"{prefix}ACCESS_KEY_SECRET"],
            endpoint=environ[f"{prefix}ENDPOINT"],
            bucket_name=bucket_name,
            base_prefix=base_prefix,
            server_side_encryption=sse,
            isolated_environment=isolated_value == "1",
        )


class OssPurgeSourceReader:
    """正式 OSS 的 Head/Get-only Adapter；不读取应用 OSS_* 配置。"""

    def __init__(self, bucket, config: PurgeSourceStorageConfig):
        self._bucket = bucket
        self.config = config

    @classmethod
    def from_env(cls, environ: Mapping[str, str]):
        config = PurgeSourceStorageConfig.from_env(environ)
        return cls(_create_bucket(config), config)

    def head(self, key: str) -> Optional[BackupObject]:
        return _head(self._bucket, key)

    def download_to(self, key: str, target: BinaryIO) -> None:
        _download(self._bucket, key, target)


class OssPurgeIsolationStorage:
    """隔离 Bucket 的 Head/Get/Put-file-only Adapter。"""

    def __init__(self, bucket, config: PurgeIsolationStorageConfig):
        self._bucket = bucket
        self.config = config

    @classmethod
    def from_env(cls, environ: Mapping[str, str]):
        config = PurgeIsolationStorageConfig.from_env(environ)
        return cls(_create_bucket(config), config)

    def head(self, key: str) -> Optional[BackupObject]:
        return _head(self._bucket, key)

    def download_to(self, key: str, target: BinaryIO) -> None:
        _download(self._bucket, key, target)

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        _validate_key(key)
        headers = {
            "x-oss-forbid-overwrite": "true",
            "x-oss-object-acl": "private",
            "x-oss-server-side-encryption": (
                self.config.server_side_encryption
            ),
        }
        headers.update(
            {
                f"x-oss-meta-{str(name).lower()}": str(value)
                for name, value in metadata.items()
            }
        )
        try:
            self._bucket.put_object_from_file(
                key,
                str(path),
                headers=headers,
            )
        except Exception as exc:
            raise _safe_storage_error(exc) from None


class FormalDeletionError(RuntimeError):
    def __init__(self, message, *, error_code):
        super().__init__(message)
        self.error_code = error_code


class OssFormalObjectDeleter:
    """Injected Head/Get/Delete adapter for the approved no-overwrite model.

    It has no ``from_env`` factory and is not imported by either production
    composition root.  T14 tests use an in-memory bucket; later authorized
    deployment work must provide the isolated credential composition.
    """

    def __init__(
        self,
        *,
        bucket,
        formal_bucket: str,
        permit_verifier,
        clock=None,
    ):
        _validate_bucket(formal_bucket)
        if not callable(permit_verifier):
            raise ValueError("formal deleter requires persisted permit verifier")
        self._bucket = bucket
        self.formal_bucket = formal_bucket
        self._permit_verifier = permit_verifier
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def observe(self, key: str) -> Optional[FormalObjectObservation]:
        _validate_key(key)
        try:
            head = self._bucket.head_object(key)
        except Exception as exc:
            if getattr(exc, "status", None) in {404, "404"}:
                return None
            raise FormalDeletionError(
                "正式对象身份读取失败",
                error_code="PURGE_OBJECT_IDENTITY_MISMATCH",
            ) from None
        headers = getattr(head, "headers", {}) or {}
        size = getattr(head, "content_length", None)
        if size is None:
            size = headers.get("Content-Length", headers.get("content-length", 0))
        etag = headers.get("ETag", headers.get("etag", ""))
        digest = hashlib.sha256()
        actual_size = 0
        try:
            response = self._bucket.get_object(key)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                actual_size += len(chunk)
                digest.update(chunk)
        except Exception:
            raise FormalDeletionError(
                "正式对象字节复验失败",
                error_code="PURGE_OBJECT_IDENTITY_MISMATCH",
            ) from None
        if int(size) != actual_size:
            raise FormalDeletionError(
                "正式对象大小复验失败",
                error_code="PURGE_OBJECT_IDENTITY_MISMATCH",
            )
        return FormalObjectObservation(
            formal_bucket=self.formal_bucket,
            formal_key=key,
            size=actual_size,
            sha256=digest.hexdigest(),
            etag=str(etag).strip('"'),
            observed_at=_aware_utc(self._clock()),
        )

    def delete(self, authorization) -> DeletionObservation:
        from services.formal_purge import DeleteCallAuthorization

        now = _aware_utc(self._clock())
        try:
            verified = self._permit_verifier(authorization)
        except Exception:
            verified = None
        if (
            not isinstance(verified, DeleteCallAuthorization)
            or verified.permit_id != getattr(authorization, 'permit_id', None)
            or verified.grant_id != getattr(authorization, 'grant_id', None)
        ):
            raise FormalDeletionError(
                "持久删除 permit 未通过执行前复验",
                error_code="PURGE_FORMAL_DELETION_DISABLED",
            )
        authorization = verified
        if (
            authorization.formal_bucket != self.formal_bucket
            or authorization.formal_key != authorization.observation.formal_key
            or _aware_utc(authorization.expires_at) <= now
        ):
            raise FormalDeletionError(
                "正式删除调用授权无效",
                error_code="PURGE_FORMAL_DELETION_DISABLED",
            )
        current = self.observe(authorization.formal_key)
        if current is None or not _same_object_identity(
            current, authorization.observation,
        ):
            raise FormalDeletionError(
                "正式对象身份在授权后变化",
                error_code="PURGE_OBJECT_IDENTITY_MISMATCH",
            )
        try:
            self._bucket.delete_object(authorization.formal_key)
        except Exception:
            raise FormalDeletionError(
                "正式对象删除失败",
                error_code="PURGE_OBJECT_DELETE_FAILED",
            ) from None
        if self.observe(authorization.formal_key) is not None:
            raise FormalDeletionError(
                "正式对象删除后仍存在",
                error_code="PURGE_OBJECT_DELETE_FAILED",
            )
        return DeletionObservation(
            result="deleted",
            before=current,
            deleted_at=now,
            after_missing=True,
        )


def _same_object_identity(left, right):
    return bool(
        isinstance(left, FormalObjectObservation)
        and isinstance(right, FormalObjectObservation)
        and left.formal_bucket == right.formal_bucket
        and left.formal_key == right.formal_key
        and left.size == right.size
        and left.sha256 == right.sha256
        and left.etag == right.etag
    )


def _aware_utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise FormalDeletionError(
            "正式删除时钟无效",
            error_code="PURGE_FORMAL_DELETION_DISABLED",
        )
    return value.astimezone(timezone.utc)


def _create_bucket(config):
    try:
        import oss2

        auth = oss2.Auth(config.access_key_id, config.access_key_secret)
        return oss2.Bucket(auth, config.endpoint, config.bucket_name)
    except ImportError as exc:  # pragma: no cover - 部署依赖检查
        raise PurgeObjectStorageConfigError("缺少 oss2 依赖") from exc
    except Exception as exc:
        raise PurgeObjectStorageConfigError(
            f"无法创建对象存储客户端: {type(exc).__name__}"
        ) from None


def _head(bucket, key: str) -> Optional[BackupObject]:
    _validate_key(key)
    try:
        result = bucket.head_object(key)
    except Exception as exc:
        if getattr(exc, "status", None) in {404, "404"}:
            return None
        raise _safe_storage_error(exc) from None
    headers = getattr(result, "headers", {}) or {}
    marker_length = len("x-oss-meta-")
    metadata = {
        str(name)[marker_length:].lower(): str(value)
        for name, value in headers.items()
        if str(name).lower().startswith("x-oss-meta-")
    }
    size = getattr(result, "content_length", None)
    if size is None:
        size = headers.get("Content-Length", headers.get("content-length", 0))
    return BackupObject(key=key, size=int(size), metadata=metadata)


def _download(bucket, key: str, target: BinaryIO) -> None:
    _validate_key(key)
    try:
        response = bucket.get_object(key)
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            target.write(chunk)
    except Exception as exc:
        raise _safe_storage_error(exc) from None


def _validate_bucket(bucket: str) -> None:
    if (
        not bucket
        or "/" in bucket
        or "\\" in bucket
        or any(ord(character) < 32 for character in bucket)
    ):
        raise PurgeObjectStorageConfigError("对象存储 Bucket 身份无效")


def _validate_key(key: str) -> None:
    if (
        not key
        or key.startswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or any(ord(character) < 32 for character in key)
    ):
        raise PurgeObjectStorageConfigError("对象 Key 或前缀不安全")


def _safe_storage_error(error: BaseException) -> BackupStorageError:
    if getattr(error, "status", None) in {409, 412, "409", "412"}:
        return BackupStorageConflictError("对象已存在且禁止覆盖")
    return BackupStorageError(
        f"对象存储操作失败: {type(error).__name__}"
    )
