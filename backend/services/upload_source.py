"""把 multipart 图片构造成确定性的请求内只读来源。"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable

from services.object_source import InMemoryObjectSource


RelativePathBuilder = Callable[[str, str, int], str]


def prepare_multipart_source(
    image_files: Iterable,
    *,
    source_bucket: str,
    build_relative_path: RelativePathBuilder,
    is_allowed: Callable[[str], bool],
    fallback_filename: str = 'upload.jpg',
):
    """读取一次 multipart 流，并以内容和同名出现序号稳定生成来源路径。"""
    objects: dict[str, bytes] = {}
    content_types: dict[str, str] = {}
    relative_paths: list[str] = []
    occurrences: dict[tuple[str, str], int] = {}

    for image_file in image_files:
        if not image_file or not is_allowed(image_file.filename or ''):
            continue
        filename = (
            (image_file.filename or '')
            .replace('\\', '/')
            .rsplit('/', 1)[-1]
            .replace('\x00', '')
            .strip()
        ) or fallback_filename
        data = image_file.read()
        content_hash = hashlib.sha256(data).hexdigest()
        occurrence_key = (filename, content_hash)
        occurrence = occurrences.get(occurrence_key, 0) + 1
        occurrences[occurrence_key] = occurrence
        relative_path = build_relative_path(
            filename,
            content_hash,
            occurrence,
        )
        objects[relative_path] = data
        content_types[relative_path] = (
            image_file.mimetype or 'application/octet-stream'
        )
        relative_paths.append(relative_path)

    return InMemoryObjectSource(
        source_bucket=source_bucket,
        objects=objects,
        content_types=content_types,
    ), relative_paths

