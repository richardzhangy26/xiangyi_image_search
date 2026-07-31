"""只读对象来源的迁移前置检查。"""

from __future__ import annotations

import re
import tempfile
import time
import warnings
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

from PIL import Image, UnidentifiedImageError

from .object_source import ReadOnlyObjectSource

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

    if head.size <= 0 or head.size > max_sample_bytes:
        raise PreflightError(
            "head_object",
            "HEAD 对象大小不在 preflight 样本限制内",
            sample_object.key,
        )

    try:
        with tempfile.TemporaryFile(mode="w+b") as downloaded_file:
            downloaded_size = source.download_object(
                sample_object.key,
                downloaded_file,
                max_bytes=max_sample_bytes,
            )
            actual_size = downloaded_file.tell()
            if downloaded_size != actual_size or head.size != actual_size:
                raise ValueError("HEAD、GetObject 返回值与实际下载字节数不一致")

            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("error", Image.DecompressionBombWarning)
                    downloaded_file.seek(0)
                    with Image.open(downloaded_file) as image:
                        image_format = image.format or "UNKNOWN"
                        image.verify()
                    downloaded_file.seek(0)
                    with Image.open(downloaded_file) as decoded:
                        decoded.load()
                        width, height = decoded.size
            except (
                Image.DecompressionBombError,
                Image.DecompressionBombWarning,
                UnidentifiedImageError,
                OSError,
                ValueError,
            ) as exc:
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
