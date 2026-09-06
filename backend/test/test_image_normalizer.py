"""搜索预览图标准化的可见行为。"""

import io

import numpy as np
import pytest
from PIL import Image

from services.image_normalizer import ImageNormalizationError, ImageNormalizer


def _save_image(path, image, image_format='PNG', **kwargs):
    image.save(path, format=image_format, **kwargs)
    return path


def _open_preview(result):
    image = Image.open(io.BytesIO(result.data))
    image.load()
    return image


def test_applies_exif_orientation(tmp_path):
    source = tmp_path / 'rotated.jpg'
    exif = Image.Exif()
    exif[274] = 6
    _save_image(
        source,
        Image.new('RGB', (40, 20), 'red'),
        image_format='JPEG',
        exif=exif,
    )

    result = ImageNormalizer().normalize(source)

    assert _open_preview(result).size == (20, 40)
    assert (result.source_width, result.source_height) == (20, 40)


def test_composites_transparency_on_white(tmp_path):
    source = tmp_path / 'transparent.png'
    _save_image(source, Image.new('RGBA', (16, 12), (0, 0, 0, 0)))

    result = ImageNormalizer().normalize(source)
    preview = _open_preview(result)

    assert preview.mode == 'RGB'
    assert all(channel >= 250 for channel in preview.getpixel((8, 6)))
    assert result.source_mime_type == 'image/png'


def test_animated_image_uses_first_valid_frame(tmp_path):
    source = tmp_path / 'animated.gif'
    first = Image.new('RGB', (18, 10), 'red')
    second = Image.new('RGB', (18, 10), 'blue')
    first.save(source, format='GIF', save_all=True, append_images=[second], loop=0)

    preview = _open_preview(ImageNormalizer().normalize(source))
    red, _green, blue = preview.getpixel((9, 5))

    assert red > 200
    assert blue < 80


def test_small_image_is_not_enlarged(tmp_path):
    source = tmp_path / 'small.png'
    _save_image(source, Image.new('RGB', (80, 40), 'green'))

    result = ImageNormalizer(max_edge=2048).normalize(source)

    assert _open_preview(result).size == (80, 40)


def test_large_image_keeps_ratio_and_limits_longest_edge(tmp_path):
    source = tmp_path / 'large.png'
    _save_image(source, Image.new('RGB', (4096, 1024), 'purple'))

    result = ImageNormalizer(max_edge=2048).normalize(source)

    assert _open_preview(result).size == (2048, 512)


def test_pixel_limit_rejects_image_before_normalization(tmp_path):
    source = tmp_path / 'too-many-pixels.png'
    _save_image(source, Image.new('RGB', (11, 10), 'orange'))

    with pytest.raises(ImageNormalizationError, match='像素'):
        ImageNormalizer(max_pixels=100).normalize(source)


def test_corrupt_image_fails_explicitly(tmp_path):
    source = tmp_path / 'corrupt.jpg'
    source.write_bytes(b'not an image')

    with pytest.raises(ImageNormalizationError, match='无法解码'):
        ImageNormalizer().normalize(source)


def test_output_is_deterministic_and_obeys_hard_byte_limit(tmp_path):
    source = tmp_path / 'noise.png'
    pixels = np.random.default_rng(42).integers(
        0,
        256,
        size=(900, 1200, 3),
        dtype=np.uint8,
    )
    _save_image(source, Image.fromarray(pixels, mode='RGB'))
    normalizer = ImageNormalizer(max_edge=2048, max_bytes=24_000)

    first = normalizer.normalize(source)
    second = normalizer.normalize(source)

    assert len(first.data) <= 24_000
    assert first.data == second.data
    assert first.width <= 1200
    assert first.height <= 900


def test_impossible_byte_limit_fails_instead_of_returning_oversized_data(tmp_path):
    source = tmp_path / 'tiny.png'
    _save_image(source, Image.new('RGB', (1, 1), 'black'))

    with pytest.raises(ImageNormalizationError, match='大小上限'):
        ImageNormalizer(max_bytes=10).normalize(source)
