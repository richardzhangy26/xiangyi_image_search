"""七牛 Kodo 的只读 S3 来源适配器与迁移前置检查。"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, BinaryIO, Iterator, Mapping, Optional, Protocol

from PIL import Image, UnidentifiedImageError

logger = logging.getLogger(__name__)

_S3_REGIONS = {
    "z0": "cn-east-1",
    "z1": "cn-north-1",
    "z2": "cn-south-1",
    "na0": "us-north-1",
    "as0": "ap-southeast-1",
    "cn-east-1": "cn-east-1",
    "cn-east-2": "cn-east-2",
    "cn-north-1": "cn-north-1",
    "cn-south-1": "cn-south-1",
    "us-north-1": "us-north-1",
    "ap-southeast-1": "ap-southeast-1",
    "ap-southeast-2": "ap-southeast-2",
    "ap-southeast-3": "ap-southeast-3",
}
_IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
_SAFE_ERROR_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")


class KodoConfigError(ValueError):
    """Kodo 环境变量缺失或不受支持。"""


@dataclass(frozen=True)
class KodoConfig:
    """只从环境变量构造的 Kodo S3 连接配置。"""

    access_key: str
    secret_key: str
    bucket_name: str
    qiniu_region: str
    s3_region: str
    endpoint_url: str
    aliases_used: tuple[str, ...] = ()
    s3_bucket_name: Optional[str] = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "KodoConfig":
        aliases_used: list[str] = []

        def read(name: str, *aliases: str) -> Optional[str]:
            value = environ.get(name)
            if value and value.strip():
                return value.strip()
            for alias in aliases:
                value = environ.get(alias)
                if value and value.strip():
                    aliases_used.append(alias)
                    return value.strip()
            return None

        access_key = read("QINIU_ACCESS_KEY", "AccessKey")
        secret_key = read("QINIU_SECRET_KEY", "SecretKey")
        bucket_name = read("QINIU_BUCKET_NAME", "BUCKET_NAME")
        qiniu_region = read("QINIU_REGION")

        missing = [
            name
            for name, value in (
                ("QINIU_ACCESS_KEY", access_key),
                ("QINIU_SECRET_KEY", secret_key),
                ("QINIU_BUCKET_NAME", bucket_name),
                ("QINIU_REGION", qiniu_region),
            )
            if not value
        ]
        if missing:
            raise KodoConfigError(f"缺少 Kodo 环境变量: {', '.join(missing)}")

        assert access_key is not None
        assert secret_key is not None
        assert bucket_name is not None
        assert qiniu_region is not None

        s3_region = _S3_REGIONS.get(qiniu_region)
        if not s3_region:
            raise KodoConfigError(
                "不支持的 QINIU_REGION；请使用已知地域简称或 S3 Region ID"
            )

        if aliases_used:
            logger.warning(
                "使用了兼容环境变量别名: %s",
                ", ".join(aliases_used),
            )

        return cls(
            access_key=access_key,
            secret_key=secret_key,
            bucket_name=bucket_name,
            qiniu_region=qiniu_region,
            s3_region=s3_region,
            endpoint_url=f"https://s3.{s3_region}.qiniucs.com",
            aliases_used=tuple(aliases_used),
            s3_bucket_name=read("QINIU_S3_BUCKET_NAME"),
        )


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
    """迁移编排器可替换的只读对象来源接口。"""

    def resolve_location(self) -> SourceLocation: ...

    def iter_objects(self, prefix: str = "") -> Iterator[SourceObject]: ...

    def head_object(self, key: str) -> SourceObjectHead: ...

    def download_object(self, key: str, target: BinaryIO) -> int: ...


class KodoS3Source:
    """使用 Kodo AWS S3 兼容 API 的只读来源。"""

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
                "无法从 Get Service 唯一解析 S3 空间名；" "请设置 QINIU_S3_BUCKET_NAME"
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

    def download_object(self, key: str, target: BinaryIO) -> int:
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


@dataclass(frozen=True)
class PreflightSample:
    key: str
    listed_size: int
    head_size: int
    downloaded_size: int
    content_type: Optional[str]
    etag: Optional[str]
    image_format: str
    width: int
    height: int


@dataclass(frozen=True)
class PreflightReport:
    source_bucket: str
    s3_bucket: str
    s3_region: str
    endpoint_url: str
    prefix: str
    total_objects: int
    image_objects: int
    total_bytes: int
    sample: PreflightSample
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PreflightError(RuntimeError):
    """带脱敏阶段信息的 preflight 失败。"""

    def __init__(
        self,
        stage: str,
        detail: str,
        object_key: Optional[str] = None,
    ):
        self.stage = stage
        self.detail = detail
        self.object_key = object_key
        super().__init__(f"{stage}: {detail}")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": "failed",
            "stage": self.stage,
            "error": self.detail,
        }
        if self.object_key is not None:
            payload["object_key"] = self.object_key
        return payload


def safe_exception_summary(error: BaseException) -> str:
    """只保留异常类型及结构化状态，不转储可能含签名或凭证的原始消息。"""
    parts = [type(error).__name__]
    response = getattr(error, "response", None)
    if isinstance(response, Mapping):
        error_data = response.get("Error")
        if isinstance(error_data, Mapping):
            code = error_data.get("Code")
            if isinstance(code, str) and _SAFE_ERROR_TOKEN.fullmatch(code):
                parts.append(f"code={code}")
        metadata = response.get("ResponseMetadata")
        if isinstance(metadata, Mapping):
            status = metadata.get("HTTPStatusCode")
            if isinstance(status, int):
                parts.append(f"http_status={status}")
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} ({', '.join(parts[1:])})"


def run_preflight(
    source: ReadOnlyObjectSource,
    *,
    prefix: str = "",
    max_sample_bytes: int = 10 * 1024 * 1024,
) -> PreflightReport:
    """扫描来源并通过一张小图片验证 HEAD、GET、字节数和图片解码。"""
    started_at = time.monotonic()
    try:
        location = source.resolve_location()
    except Exception as exc:
        raise PreflightError(
            "resolve_bucket",
            safe_exception_summary(exc),
        ) from exc

    try:
        objects = list(source.iter_objects(prefix))
    except Exception as exc:
        raise PreflightError(
            "list_objects",
            safe_exception_summary(exc),
        ) from exc

    image_objects = [item for item in objects if _is_image_key(item.key)]
    candidates = [item for item in image_objects if 0 < item.size <= max_sample_bytes]
    if not candidates:
        raise PreflightError(
            "select_sample",
            "未找到符合大小限制的非空图片对象",
        )
    sample_object = min(candidates, key=lambda item: (item.size, item.key))

    try:
        head = source.head_object(sample_object.key)
    except Exception as exc:
        raise PreflightError(
            "head_object",
            safe_exception_summary(exc),
            sample_object.key,
        ) from exc

    try:
        with tempfile.TemporaryFile(mode="w+b") as downloaded_file:
            downloaded_size = source.download_object(
                sample_object.key,
                downloaded_file,
            )
            actual_size = downloaded_file.tell()
            if downloaded_size != actual_size or head.size != actual_size:
                raise ValueError("HEAD、GetObject 返回值与实际下载字节数不一致")

            try:
                downloaded_file.seek(0)
                with Image.open(downloaded_file) as image:
                    image_format = image.format or "UNKNOWN"
                    image.verify()
                downloaded_file.seek(0)
                with Image.open(downloaded_file) as decoded:
                    decoded.load()
                    width, height = decoded.size
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise PreflightError(
                    "decode_image",
                    safe_exception_summary(exc),
                    sample_object.key,
                ) from exc
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError(
            "download_object",
            safe_exception_summary(exc),
            sample_object.key,
        ) from exc

    return PreflightReport(
        source_bucket=location.source_bucket,
        s3_bucket=location.s3_bucket,
        s3_region=location.s3_region,
        endpoint_url=location.endpoint_url,
        prefix=prefix,
        total_objects=len(objects),
        image_objects=len(image_objects),
        total_bytes=sum(item.size for item in objects),
        sample=PreflightSample(
            key=sample_object.key,
            listed_size=sample_object.size,
            head_size=head.size,
            downloaded_size=downloaded_size,
            content_type=head.content_type,
            etag=head.etag,
            image_format=image_format,
            width=width,
            height=height,
        ),
        elapsed_seconds=round(time.monotonic() - started_at, 3),
    )


def _is_image_key(key: str) -> bool:
    filename = key.rsplit("/", 1)[-1]
    if "." not in filename:
        return False
    return f".{filename.rsplit('.', 1)[-1].lower()}" in _IMAGE_EXTENSIONS
