"""源图到搜索预览图的确定性标准化。"""

from __future__ import annotations

import io
import math
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from PIL import Image, ImageOps, UnidentifiedImageError

DEFAULT_MAX_EDGE = 2048
DEFAULT_MAX_BYTES = int(2.5 * 1024 * 1024)
DEFAULT_MAX_PIXELS = 100_000_000
DEFAULT_NORMALIZATION_VERSION = 'preview-v1'
_JPEG_QUALITIES = (95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40, 35)


class ImageNormalizationError(ValueError):
    """源图无法安全、确定地转换为搜索预览图。"""


@dataclass(frozen=True)
class NormalizedImage:
    data: bytes
    width: int
    height: int
    source_width: int
    source_height: int
    source_mime_type: str
    normalization_version: str


class ImageNormalizer:
    """生成最长边和文件大小均受限的白底 JPEG 搜索预览图。"""

    def __init__(
        self,
        *,
        max_edge: int = DEFAULT_MAX_EDGE,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        normalization_version: str = DEFAULT_NORMALIZATION_VERSION,
    ):
        if max_edge <= 0:
            raise ValueError('max_edge 必须大于 0')
        if max_bytes <= 0:
            raise ValueError('max_bytes 必须大于 0')
        if max_pixels <= 0:
            raise ValueError('max_pixels 必须大于 0')
        if not normalization_version:
            raise ValueError('normalization_version 不能为空')

        self.max_edge = int(max_edge)
        self.max_bytes = int(max_bytes)
        self.max_pixels = int(max_pixels)
        self.normalization_version = normalization_version

    @classmethod
    def from_env(cls, environ=None):
        environment = environ if environ is not None else os.environ
        try:
            max_edge = int(environment.get('IMAGE_PREVIEW_MAX_EDGE', DEFAULT_MAX_EDGE))
            max_bytes = int(
                float(environment.get('IMAGE_PREVIEW_MAX_MB', 2.5)) * 1024 * 1024
            )
            max_pixels = int(
                environment.get('IMAGE_MAX_PIXELS', DEFAULT_MAX_PIXELS)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError('图片标准化环境变量格式无效') from exc

        return cls(
            max_edge=max_edge,
            max_bytes=max_bytes,
            max_pixels=max_pixels,
            normalization_version=environment.get(
                'IMAGE_NORMALIZATION_VERSION',
                DEFAULT_NORMALIZATION_VERSION,
            ),
        )

    def normalize(self, source_path: Union[str, Path]) -> NormalizedImage:
        """读取一张源图，返回可直接持久化和送入 embedding 的 JPEG。"""
        try:
            with warnings.catch_warnings():
                # 使用本类更严格且可配置的像素上限；避免依赖 Pillow 全局状态。
                warnings.simplefilter('ignore', Image.DecompressionBombWarning)
                with Image.open(source_path) as opened:
                    image_format = opened.format
                    self._validate_pixel_count(*opened.size)
                    frame = self._first_valid_frame(opened)
        except ImageNormalizationError:
            raise
        except (
            Image.DecompressionBombError,
            UnidentifiedImageError,
            OSError,
            ValueError,
        ) as exc:
            raise ImageNormalizationError(
                f'无法解码源图片: {type(exc).__name__}'
            ) from exc

        transposed = ImageOps.exif_transpose(frame)
        if transposed is None:  # 仅 in_place=True 时才可能出现；这里防御类型契约漂移。
            raise ImageNormalizationError('EXIF 方向处理失败')
        frame = transposed
        source_width, source_height = frame.size
        self._validate_pixel_count(source_width, source_height)

        rgb = self._to_white_background_rgb(frame)
        normalized = self._limit_longest_edge(rgb)
        data, normalized = self._encode_with_hard_limit(normalized)

        source_mime_type = Image.MIME.get(image_format)
        if not source_mime_type or not source_mime_type.startswith('image/'):
            raise ImageNormalizationError('无法确定源图片的媒体类型')

        return NormalizedImage(
            data=data,
            width=normalized.width,
            height=normalized.height,
            source_width=source_width,
            source_height=source_height,
            source_mime_type=source_mime_type,
            normalization_version=self.normalization_version,
        )

    def _validate_pixel_count(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise ImageNormalizationError('图片尺寸无效')
        if width * height > self.max_pixels:
            raise ImageNormalizationError(
                f'图片像素数超过安全上限 {self.max_pixels}'
            )

    @staticmethod
    def _first_valid_frame(opened: Image.Image) -> Image.Image:
        last_error = None
        frame_count = max(1, int(getattr(opened, 'n_frames', 1)))
        for frame_index in range(frame_count):
            try:
                opened.seek(frame_index)
                frame = opened.copy()
                frame.load()
                return frame
            except (EOFError, OSError, ValueError) as exc:
                last_error = exc
        raise ImageNormalizationError(
            f'动图中没有可解码画面: {type(last_error).__name__}'
        )

    @staticmethod
    def _to_white_background_rgb(image: Image.Image) -> Image.Image:
        has_transparency = (
            image.mode in ('RGBA', 'LA')
            or (image.mode == 'P' and 'transparency' in image.info)
        )
        if has_transparency:
            foreground = image.convert('RGBA')
            background = Image.new('RGBA', foreground.size, (255, 255, 255, 255))
            return Image.alpha_composite(background, foreground).convert('RGB')
        return image.convert('RGB')

    def _limit_longest_edge(self, image: Image.Image) -> Image.Image:
        longest_edge = max(image.size)
        if longest_edge <= self.max_edge:
            return image
        scale = self.max_edge / longest_edge
        size = (
            max(1, round(image.width * scale)),
            max(1, round(image.height * scale)),
        )
        return image.resize(size, Image.Resampling.LANCZOS)

    def _encode_with_hard_limit(
        self,
        image: Image.Image,
    ) -> tuple[bytes, Image.Image]:
        current = image
        while True:
            smallest = b''
            for quality in _JPEG_QUALITIES:
                output = io.BytesIO()
                try:
                    current.save(
                        output,
                        format='JPEG',
                        quality=quality,
                        optimize=True,
                        progressive=False,
                        subsampling=2,
                    )
                except OSError as exc:
                    raise ImageNormalizationError(
                        f'搜索预览图编码失败: {type(exc).__name__}'
                    ) from exc
                smallest = output.getvalue()
                if len(smallest) <= self.max_bytes:
                    return smallest, current

            if current.size == (1, 1):
                raise ImageNormalizationError(
                    f'无法满足搜索预览图大小上限 {self.max_bytes} 字节'
                )

            ratio = math.sqrt(self.max_bytes / len(smallest)) * 0.9
            scale = min(0.85, ratio)
            new_size = (
                max(1, min(current.width - 1, round(current.width * scale))),
                max(1, min(current.height - 1, round(current.height * scale))),
            )
            current = current.resize(new_size, Image.Resampling.LANCZOS)
