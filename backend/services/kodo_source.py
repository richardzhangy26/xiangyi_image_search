"""使用七牛 Kodo S3 兼容 API 的只读来源适配器。"""

from __future__ import annotations

import logging
from typing import Any, BinaryIO, Iterator, Mapping, Optional

from .kodo_config import KodoConfig
from .object_source import SourceLocation, SourceObject, SourceObjectHead

logger = logging.getLogger(__name__)


class DownloadSizeLimitExceeded(RuntimeError):
    """GetObject 数据流超过调用方声明的只读下载上限。"""


class KodoS3Source:
    """只执行 Get Service、ListObjectsV2、HEAD 和 GET 的 Kodo 来源。"""

    def __init__(self, config: KodoConfig, client: Any = None):
        self.config = config
        self._client = client if client is not None else _create_s3_client(config)
        self._resolved_bucket: Optional[str] = None

    def resolve_location(self) -> SourceLocation:
        response = self._client.list_buckets()
        bucket_names: list[str] = []
        for item in response.get("Buckets", []):
            if not isinstance(item, Mapping):
                continue
            name = item.get("Name")
            if isinstance(name, str) and name:
                bucket_names.append(name)

        requested = self.config.s3_bucket_name or self.config.bucket_name
        if requested in bucket_names:
            resolved = requested
        elif self.config.s3_bucket_name:
            raise LookupError("配置的 QINIU_S3_BUCKET_NAME 不在 Get Service 结果中")
        elif len(bucket_names) == 1:
            resolved = bucket_names[0]
            logger.info("Kodo 空间名通过 Get Service 解析为唯一可访问的 S3 空间名")
        else:
            raise LookupError(
                "无法从 Get Service 唯一解析 S3 空间名；请设置 QINIU_S3_BUCKET_NAME"
            )

        self._resolved_bucket = str(resolved)
        return SourceLocation(
            source_bucket=self.config.bucket_name,
            s3_bucket=self._resolved_bucket,
            s3_region=self.config.s3_region,
            endpoint_url=self.config.endpoint_url,
        )

    def iter_objects(self, prefix: str = "") -> Iterator[SourceObject]:
        bucket = self._require_resolved_bucket()
        continuation_token: Optional[str] = None

        while True:
            request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if continuation_token:
                request["ContinuationToken"] = continuation_token
            response = self._client.list_objects_v2(**request)

            for item in response.get("Contents", []):
                key = item.get("Key")
                if not isinstance(key, str):
                    continue
                yield SourceObject(
                    key=key,
                    size=int(item.get("Size", 0)),
                    etag=item.get("ETag"),
                    last_modified=item.get("LastModified"),
                )

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                raise RuntimeError("分页响应缺少 NextContinuationToken")

    def head_object(self, key: str) -> SourceObjectHead:
        response = self._client.head_object(
            Bucket=self._require_resolved_bucket(),
            Key=key,
        )
        return SourceObjectHead(
            key=key,
            size=int(response.get("ContentLength", 0)),
            content_type=response.get("ContentType"),
            etag=response.get("ETag"),
        )

    def download_object(
        self,
        key: str,
        target: BinaryIO,
        *,
        max_bytes: Optional[int] = None,
    ) -> int:
        response = self._client.get_object(
            Bucket=self._require_resolved_bucket(),
            Key=key,
        )
        body = response.get("Body")
        if body is None:
            raise RuntimeError("GetObject 响应缺少 Body")

        downloaded = 0
        try:
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                if max_bytes is not None and downloaded + len(chunk) > max_bytes:
                    raise DownloadSizeLimitExceeded(
                        "GetObject 数据流超过 preflight 样本上限"
                    )
                target.write(chunk)
                downloaded += len(chunk)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        return downloaded

    def _require_resolved_bucket(self) -> str:
        if self._resolved_bucket is None:
            return self.resolve_location().s3_bucket
        return self._resolved_bucket


def _create_s3_client(config: KodoConfig) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - 由部署依赖检查覆盖
        raise RuntimeError("缺少 boto3，无法创建 Kodo S3 客户端") from exc

    return boto3.client(
        "s3",
        aws_access_key_id=config.access_key,
        aws_secret_access_key=config.secret_key,
        endpoint_url=config.endpoint_url,
        region_name=config.s3_region,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )
