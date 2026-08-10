"""独立数据库备份桶的最小权限对象存储边界。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Mapping, Optional, Protocol


class BackupStorageError(RuntimeError):
    """备份对象存储失败；消息不得包含 SDK 响应或凭证。"""


class BackupStorageConfigError(BackupStorageError):
    """专用备份存储配置不安全或不完整。"""


class BackupStorageConflictError(BackupStorageError):
    """不可覆盖的备份对象已存在。"""


@dataclass(frozen=True)
class BackupObject:
    key: str
    size: int
    metadata: Mapping[str, str]


@dataclass(frozen=True)
class BackupStorageConfig:
    access_key_id: str
    access_key_secret: str
    endpoint: str
    bucket_name: str
    base_prefix: str
    server_side_encryption: str

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "BackupStorageConfig":
        required = (
            "BACKUP_OSS_ACCESS_KEY_ID",
            "BACKUP_OSS_ACCESS_KEY_SECRET",
            "BACKUP_OSS_ENDPOINT",
            "BACKUP_OSS_BUCKET_NAME",
        )
        missing = [name for name in required if not environ.get(name)]
        if missing:
            raise BackupStorageConfigError(
                f"缺少专用备份存储配置: {', '.join(missing)}"
            )

        bucket_name = environ["BACKUP_OSS_BUCKET_NAME"]
        if bucket_name == environ.get("OSS_BUCKET_NAME"):
            raise BackupStorageConfigError("备份 Bucket 必须独立于正式图片 Bucket")
        access_key_id = environ["BACKUP_OSS_ACCESS_KEY_ID"]
        if access_key_id == environ.get("OSS_ACCESS_KEY_ID"):
            raise BackupStorageConfigError("备份写入凭证必须独立于应用运行凭证")
        if access_key_id in {
            value
            for value in (
                environ.get("PURGE_SOURCE_OSS_ACCESS_KEY_ID"),
                environ.get("PURGE_RESTORE_OSS_ACCESS_KEY_ID"),
            )
            if value
        }:
            raise BackupStorageConfigError(
                "备份写入凭证必须独立于清除源读取和隔离恢复凭证"
            )
        if bucket_name in {
            value
            for value in (
                environ.get("PURGE_SOURCE_OSS_BUCKET_NAME"),
                environ.get("PURGE_RESTORE_OSS_BUCKET_NAME"),
            )
            if value
        }:
            raise BackupStorageConfigError(
                "备份 Bucket 必须独立于正式源和隔离恢复 Bucket"
            )

        base_prefix = environ.get(
            "BACKUP_OSS_BASE_PREFIX", "postgresql-backups"
        ).strip("/")
        _validate_key(base_prefix)
        sse = environ.get("BACKUP_OSS_SSE", "AES256")
        if sse not in {"AES256", "KMS"}:
            raise BackupStorageConfigError("BACKUP_OSS_SSE 只允许 AES256 或 KMS")

        return cls(
            access_key_id=access_key_id,
            access_key_secret=environ["BACKUP_OSS_ACCESS_KEY_SECRET"],
            endpoint=environ["BACKUP_OSS_ENDPOINT"],
            bucket_name=bucket_name,
            base_prefix=base_prefix,
            server_side_encryption=sse,
        )


class BackupStorage(Protocol):
    def head(self, key: str) -> Optional[BackupObject]:
        ...

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        ...

    def put_bytes_if_absent(
        self,
        key: str,
        data: bytes,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        ...

    def download_to(self, key: str, target: BinaryIO) -> None:
        ...


class OssBackupStorage:
    """只具备 Put/Head/Get 的数据库备份 OSS 适配器。"""

    def __init__(self, bucket, config: BackupStorageConfig):
        self._bucket = bucket
        self.config = config

    @classmethod
    def from_env(cls, environ: Mapping[str, str]):
        config = BackupStorageConfig.from_env(environ)
        try:
            import oss2

            auth = oss2.Auth(config.access_key_id, config.access_key_secret)
            bucket = oss2.Bucket(auth, config.endpoint, config.bucket_name)
        except ImportError as exc:  # pragma: no cover - 部署前置检查
            raise BackupStorageConfigError("缺少 oss2 依赖") from exc
        except Exception as exc:
            raise BackupStorageConfigError(
                f"无法创建备份存储客户端: {type(exc).__name__}"
            ) from None
        return cls(bucket, config)

    def head(self, key: str) -> Optional[BackupObject]:
        _validate_key(key)
        try:
            result = self._bucket.head_object(key)
        except Exception as exc:
            if getattr(exc, "status", None) in {404, "404"}:
                return None
            raise _safe_storage_error(exc) from None

        headers = getattr(result, "headers", {}) or {}
        metadata_prefix_length = len("x-oss-meta-")
        metadata = {
            str(name)[metadata_prefix_length:].lower(): str(value)
            for name, value in headers.items()
            if str(name).lower().startswith("x-oss-meta-")
        }
        size = getattr(result, "content_length", None)
        if size is None:
            size = headers.get("Content-Length", headers.get("content-length", 0))
        return BackupObject(key=key, size=int(size), metadata=metadata)

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        _validate_key(key)
        try:
            self._bucket.put_object_from_file(
                key,
                str(path),
                headers=self._headers(metadata),
            )
        except Exception as exc:
            raise _safe_storage_error(exc) from None

    def put_bytes_if_absent(
        self,
        key: str,
        data: bytes,
        *,
        metadata: Mapping[str, str],
    ) -> None:
        _validate_key(key)
        try:
            self._bucket.put_object(
                key,
                data,
                headers=self._headers(metadata),
            )
        except Exception as exc:
            raise _safe_storage_error(exc) from None

    def download_to(self, key: str, target: BinaryIO) -> None:
        _validate_key(key)
        try:
            response = self._bucket.get_object(key)
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                target.write(chunk)
        except Exception as exc:
            raise _safe_storage_error(exc) from None

    def object_key(self, backup_id: str, file_name: str) -> str:
        _validate_identifier(backup_id)
        _validate_identifier(file_name)
        return f"{self.config.base_prefix}/{backup_id}/{file_name}"

    def _headers(self, metadata: Mapping[str, str]) -> dict[str, str]:
        headers = {
            "x-oss-forbid-overwrite": "true",
            "x-oss-object-acl": "private",
            "x-oss-server-side-encryption": self.config.server_side_encryption,
        }
        headers.update(
            {
                f"x-oss-meta-{str(name).lower()}": str(value)
                for name, value in metadata.items()
            }
        )
        return headers


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _validate_identifier(value: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise BackupStorageConfigError("备份对象标识包含不安全字符")


def _validate_key(key: str) -> None:
    if (
        not key
        or key.startswith("/")
        or "\\" in key
        or any(part in {"", ".", ".."} for part in key.split("/"))
        or any(ord(character) < 32 for character in key)
    ):
        raise BackupStorageConfigError("备份对象键不安全")


def _safe_storage_error(error: BaseException) -> BackupStorageError:
    if getattr(error, "status", None) in {409, 412, "409", "412"}:
        return BackupStorageConflictError("备份对象已存在且禁止覆盖")
    return BackupStorageError(f"备份对象存储操作失败: {type(error).__name__}")
