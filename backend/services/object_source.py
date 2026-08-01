"""迁移编排器使用的只读对象来源契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Iterator, Mapping, Optional, Protocol


@dataclass(frozen=True)
class SourceLocation:
    source_bucket: str
    s3_bucket: str
    s3_region: str
    endpoint_url: str


@dataclass(frozen=True)
class SourceObject:
    key: str
    size: int
    etag: Optional[str] = None
    last_modified: Optional[datetime] = None


@dataclass(frozen=True)
class SourceObjectHead:
    key: str
    size: int
    content_type: Optional[str] = None
    etag: Optional[str] = None


class ReadOnlyObjectSource(Protocol):
    """后续迁移编排器可替换、可注入 fake 的只读来源接口。"""

    def resolve_location(self) -> SourceLocation:
        ...

    def iter_objects(self, prefix: str = "") -> Iterator[SourceObject]:
        ...

    def head_object(self, key: str) -> SourceObjectHead:
        ...

    def download_object(
        self,
        key: str,
        target: BinaryIO,
        *,
        max_bytes: Optional[int] = None,
    ) -> int:
        ...


class InMemoryObjectSource:
    """把一次 HTTP 上传包装成只读来源，供统一图片资产入库服务消费。

    对象只在当前请求内存在；正式源图仍由入库服务写入私有 OSS。
    """

    def __init__(
        self,
        *,
        source_bucket: str,
        objects: Mapping[str, bytes],
        content_types: Optional[Mapping[str, str]] = None,
    ):
        if not source_bucket:
            raise ValueError('来源命名空间不能为空')
        self._source_bucket = source_bucket
        self._objects = {
            str(key): bytes(value)
            for key, value in objects.items()
        }
        self._content_types = {
            str(key): str(value)
            for key, value in (content_types or {}).items()
        }

    def resolve_location(self) -> SourceLocation:
        return SourceLocation(
            source_bucket=self._source_bucket,
            s3_bucket='',
            s3_region='',
            endpoint_url='',
        )

    def iter_objects(self, prefix: str = "") -> Iterator[SourceObject]:
        for key, data in self._objects.items():
            if key.startswith(prefix):
                yield SourceObject(key=key, size=len(data))

    def head_object(self, key: str) -> SourceObjectHead:
        data = self._require_object(key)
        return SourceObjectHead(
            key=key,
            size=len(data),
            content_type=self._content_types.get(key),
        )

    def download_object(
        self,
        key: str,
        target: BinaryIO,
        *,
        max_bytes: Optional[int] = None,
    ) -> int:
        data = self._require_object(key)
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError('上传图片超过读取上限')
        target.write(data)
        return len(data)

    def _require_object(self, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError as exc:
            raise FileNotFoundError('上传图片不存在') from exc
