"""私有阿里云 OSS 对象存储边界。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Protocol, Union


class ObjectStorageError(RuntimeError):
    """对象存储操作失败；错误文本不得包含凭证或签名 URL。"""


class ObjectStorageConfigError(ObjectStorageError):
    """对象存储环境配置不完整。"""


class ObjectStorageConflictError(ObjectStorageError):
    """目标 Object Key 已存在且不能安全复用。"""


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    content_type: Optional[str]
    metadata: Mapping[str, str]


class ObjectStorage(Protocol):
    def head_object(self, key: str) -> Optional[StoredObject]: ...

    def put_file(
        self,
        key: str,
        source_path: Union[str, Path],
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> None: ...

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> None: ...

    def sign_download_url(self, key: str, expires_seconds: int) -> str: ...


class OssObjectStorage:
    """只使用私有对象读写和短时签名，不修改 Bucket ACL。"""

    def __init__(self, bucket):
        self._bucket = bucket

    @classmethod
    def from_env(cls, environ=None):
        environment = environ if environ is not None else os.environ
        required_names = (
            'OSS_ACCESS_KEY_ID',
            'OSS_ACCESS_KEY_SECRET',
            'OSS_ENDPOINT',
            'OSS_BUCKET_NAME',
        )
        missing = [name for name in required_names if not environment.get(name)]
        if missing:
            raise ObjectStorageConfigError(
                f"缺少 OSS 配置: {', '.join(missing)}"
            )

        try:
            import oss2

            auth = oss2.Auth(
                environment['OSS_ACCESS_KEY_ID'],
                environment['OSS_ACCESS_KEY_SECRET'],
            )
            bucket = oss2.Bucket(
                auth,
                environment['OSS_ENDPOINT'],
                environment['OSS_BUCKET_NAME'],
            )
        except ImportError as exc:  # pragma: no cover - 部署依赖检查覆盖
            raise ObjectStorageConfigError('缺少 oss2 依赖') from exc
        except Exception as exc:
            raise ObjectStorageConfigError(
                f'无法创建 OSS 客户端: {type(exc).__name__}'
            ) from exc
        return cls(bucket)

    def head_object(self, key: str) -> Optional[StoredObject]:
        try:
            result = self._bucket.head_object(key)
        except Exception as exc:
            if getattr(exc, 'status', None) == 404:
                return None
            raise ObjectStorageError(
                f'OSS HEAD 失败: {type(exc).__name__}'
            ) from exc

        headers = getattr(result, 'headers', {}) or {}
        metadata = {
            str(name)[len('x-oss-meta-'):].lower(): str(value)
            for name, value in headers.items()
            if str(name).lower().startswith('x-oss-meta-')
        }
        size = getattr(result, 'content_length', None)
        if size is None:
            size = headers.get('Content-Length', headers.get('content-length', 0))
        content_type = getattr(result, 'content_type', None)
        if content_type is None:
            content_type = headers.get('Content-Type', headers.get('content-type'))
        return StoredObject(
            key=key,
            size=int(size),
            content_type=content_type,
            metadata=metadata,
        )

    def put_file(
        self,
        key: str,
        source_path: Union[str, Path],
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> None:
        try:
            self._bucket.put_object_from_file(
                key,
                str(source_path),
                headers=self._upload_headers(content_type, metadata),
            )
        except Exception as exc:
            raise self._safe_upload_error(exc) from exc

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        metadata: Mapping[str, str],
    ) -> None:
        try:
            self._bucket.put_object(
                key,
                data,
                headers=self._upload_headers(content_type, metadata),
            )
        except Exception as exc:
            raise self._safe_upload_error(exc) from exc

    def sign_download_url(self, key: str, expires_seconds: int) -> str:
        if expires_seconds <= 0:
            raise ObjectStorageConfigError('OSS 签名有效期必须大于 0')
        try:
            return self._bucket.sign_url(
                'GET',
                key,
                expires_seconds,
                slash_safe=True,
            )
        except Exception as exc:
            raise ObjectStorageError(
                f'OSS 签名失败: {type(exc).__name__}'
            ) from exc

    @staticmethod
    def _upload_headers(
        content_type: str,
        metadata: Mapping[str, str],
    ) -> dict[str, str]:
        headers = {
            'Content-Type': content_type,
            # 即使 HEAD 与 PUT 之间出现竞态，也绝不覆盖已有对象。
            'x-oss-forbid-overwrite': 'true',
        }
        headers.update({
            f'x-oss-meta-{str(name).lower()}': str(value)
            for name, value in metadata.items()
        })
        return headers

    @staticmethod
    def _safe_upload_error(error: BaseException) -> ObjectStorageError:
        status = getattr(error, 'status', None)
        if status in (409, 412):
            return ObjectStorageConflictError(
                f'OSS 对象已存在: {type(error).__name__}'
            )
        return ObjectStorageError(f'OSS 上传失败: {type(error).__name__}')
