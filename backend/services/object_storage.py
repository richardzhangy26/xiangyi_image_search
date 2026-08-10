"""私有阿里云 OSS 对象存储边界。"""

from __future__ import annotations

import base64
import os
import time
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
class SignedDownloadUrl:
    """带过期时刻的签名下载地址；同一对象在同一时间窗口内 URL 稳定。"""

    url: str
    expires_at: int  # epoch 秒


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


class PrivateObjectSigner(Protocol):
    def sign_download_url(
        self,
        key: str,
        expires_seconds: int,
        *,
        cache_control: Optional[str] = None,
    ) -> SignedDownloadUrl:
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

    def sign_download_url(
        self,
        key: str,
        expires_seconds: int,
        *,
        cache_control: Optional[str] = None,
    ) -> SignedDownloadUrl:
        if expires_seconds <= 0:
            raise ObjectStorageConfigError('OSS 签名有效期必须大于 0')
        params = (
            {'response-cache-control': cache_control}
            if cache_control
            else None
        )
        # 过期时刻对齐到 expires_seconds 长度的固定时间窗口终点，而不是
        # “调用时刻 + TTL”：同一对象在同一窗口内生成的签名 URL 完全一致，
        # 浏览器刷新时可按 URL 命中缓存，避免重复消耗 OSS 出口流量。
        # oss2 内部以 int(time.time()) + expires 计算过期时刻，签名计算与
        # 取值之间跨秒时 URL 可能差 1 秒；调用方需用缓存余量吸收该误差。
        now = int(time.time())
        expires_at = (now // expires_seconds + 1) * expires_seconds
        try:
            url = self._bucket.sign_url(
                'GET',
                key,
                expires_at - now,
                slash_safe=True,
                params=params,
            )
        except Exception as exc:
            raise ObjectStorageError(
                f'OSS 签名失败: {type(exc).__name__}'
            ) from None
        return SignedDownloadUrl(url=url, expires_at=expires_at)

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
