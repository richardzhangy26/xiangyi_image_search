"""迁移编排器使用的只读对象来源契约。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Iterator, Optional, Protocol


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

    def resolve_location(self) -> SourceLocation: ...

    def iter_objects(self, prefix: str = "") -> Iterator[SourceObject]: ...

    def head_object(self, key: str) -> SourceObjectHead: ...

    def download_object(
        self,
        key: str,
        target: BinaryIO,
        *,
        max_bytes: Optional[int] = None,
    ) -> int: ...
