"""私有阿里云 OSS 对象存储边界。"""

from __future__ import annotations

import base64
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
    etag: Optional[str]


@dataclass(frozen=True)
class ObjectSpec:
    """上传与 HEAD 复用校验所需的完整对象契约。"""

    size: int
    content_type: str
    metadata: Mapping[str, str]
    md5_hex: str

    @property
    def content_md5(self) -> str:
        return base64.b64encode(bytes.fromhex(self.md5_hex)).decode('ascii')


@dataclass(frozen=True)
class ObjectStorageTargetInspection:
    """写入前只读获取的 OSS 目标身份、ACL 与前缀样本。"""

    bucket_name: str
    location: str
    acl: str
    sample_key: Optional[str]
    sample_metadata: Mapping[str, str]


class ObjectWriter(Protocol):
    def head_object(self, key: str) -> Optional[StoredObject]:
        ...

    def put_file(
        self,
        key: str,
        source_path: Union[str, Path],
        *,
        spec: ObjectSpec,
    ) -> None:
        ...

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        spec: ObjectSpec,
    ) -> None:
        ...


class ObjectReader(Protocol):
    def download_file(
        self,
        key: str,
        target_path: Union[str, Path],
    ) -> None:
        ...


class ObjectCleaner(Protocol):
    """引用安全清理所需的私有对象删除能力；仅供受控清理路径使用。"""

    def delete_object(self, key: str) -> str:
        ...


class PrivateObjectSigner(Protocol):
    def sign_download_url(self, key: str, expires_seconds: int) -> str:
        ...


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
            ) from None

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
        etag = getattr(result, 'etag', None)
        if etag is None:
            etag = headers.get('ETag', headers.get('etag'))
        return StoredObject(
            key=key,
            size=int(size),
            content_type=content_type,
            metadata=metadata,
            etag=str(etag) if etag is not None else None,
        )

    def inspect_target(
        self,
        base_prefix: str,
    ) -> ObjectStorageTargetInspection:
        """只读核对 Bucket 信息，并抽查隔离前缀中的一个既有对象。"""
        normalized_prefix = base_prefix.strip('/')
        try:
            info = self._bucket.get_bucket_info()
            listed = self._bucket.list_objects_v2(
                prefix=f'{normalized_prefix}/',
                max_keys=1,
            )
        except Exception as exc:
            raise ObjectStorageError(
                f'OSS 目标预检失败: {type(exc).__name__}'
            ) from None

        acl_value = getattr(getattr(info, 'acl', None), 'grant', None)
        object_list = getattr(listed, 'object_list', None) or []
        sample_key = (
            str(getattr(object_list[0], 'key', ''))
            if object_list
            else None
        )
        sample = self.head_object(sample_key) if sample_key else None
        return ObjectStorageTargetInspection(
            bucket_name=str(
                getattr(info, 'name', None)
                or getattr(self._bucket, 'bucket_name', '')
            ),
            location=str(getattr(info, 'location', '') or ''),
            acl=str(acl_value or ''),
            sample_key=sample_key,
            sample_metadata=dict(sample.metadata) if sample else {},
        )

    def put_file(
        self,
        key: str,
        source_path: Union[str, Path],
        *,
        spec: ObjectSpec,
    ) -> None:
        try:
            self._bucket.put_object_from_file(
                key,
                str(source_path),
                headers=self._upload_headers(spec),
            )
        except Exception as exc:
            raise self._safe_upload_error(exc) from None

    def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        spec: ObjectSpec,
    ) -> None:
        try:
            self._bucket.put_object(
                key,
                data,
                headers=self._upload_headers(spec),
            )
        except Exception as exc:
            raise self._safe_upload_error(exc) from None

    def download_file(
        self,
        key: str,
        target_path: Union[str, Path],
    ) -> None:
        """把私有对象直接下载到 worker 指定的临时文件。"""
        try:
            self._bucket.get_object_to_file(key, str(target_path))
        except Exception as exc:
            error = ObjectStorageError(
                f'OSS 下载失败: {type(exc).__name__}'
            )
            error.stage = 'download'
            status = getattr(exc, 'status', None)
            if isinstance(status, int):
                error.status_code = status
            elif isinstance(status, str) and status.isdigit():
                error.status_code = int(status)
            raise error from None

    def delete_object(self, key: str) -> str:
        """删除私有对象；对象已不存在视为成功（幂等）。

        返回 'deleted' 或 'already_gone'；其他失败抛 ObjectStorageError。
        仅供受控清理路径调用，绝不用于迁移或覆盖语义。
        """
        try:
            self._bucket.delete_object(key)
            return 'deleted'
        except Exception as exc:
            status = getattr(exc, 'status', None)
            if status in (404, '404') or type(exc).__name__ == 'NoSuchKey':
                return 'already_gone'
            error = ObjectStorageError(
                f'OSS 对象删除失败: {type(exc).__name__}'
            )
            error.stage = 'delete'
            raise error from None

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
            ) from None

    @staticmethod
    def _upload_headers(spec: ObjectSpec) -> dict[str, str]:
        headers = {
            'Content-Type': spec.content_type,
            # OSS 会验证请求体 MD5，避免传输成功但对象内容已损坏。
            'Content-MD5': spec.content_md5,
            # 即使 HEAD 与 PUT 之间出现竞态，也绝不覆盖已有对象。
            'x-oss-forbid-overwrite': 'true',
        }
        headers.update({
            f'x-oss-meta-{str(name).lower()}': str(value)
            for name, value in spec.metadata.items()
        })
        return headers

    @staticmethod
    def _safe_upload_error(error: BaseException) -> ObjectStorageError:
        status = getattr(error, 'status', None)
        if status in (409, 412, '409', '412'):
            return ObjectStorageConflictError(
                f'OSS 对象已存在: {type(error).__name__}'
            )
        return ObjectStorageError(f'OSS 上传失败: {type(error).__name__}')
