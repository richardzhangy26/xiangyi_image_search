"""单张来源图片到私有 OSS 与独立图片资产表的入库闭环。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from models import ImageAsset, db
from services.embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EmbeddingClient,
)
from services.image_normalizer import ImageNormalizer
from services.object_source import ReadOnlyObjectSource
from services.object_storage import ObjectStorage, StoredObject

SOURCE_PROVIDER = 'qiniu-kodo'
DEFAULT_OSS_IMAGE_BASE_PREFIX = 'image-search'


class AssetIngestError(RuntimeError):
    """单张图片资产入库失败。"""


class AssetIngestConflictError(AssetIngestError):
    """来源或目标对象已存在，但内容不允许安全复用。"""


@dataclass(frozen=True)
class AssetIngestResult:
    status: str
    asset_id: str
    content_hash: str
    oss_path: str
    preview_oss_path: str


class ImageAssetIngestService:
    """编排一个来源对象的下载、标准化、上传、向量化和持久化。"""

    def __init__(
        self,
        *,
        source: ReadOnlyObjectSource,
        storage: ObjectStorage,
        embedding_client=None,
        normalizer: Optional[ImageNormalizer] = None,
        oss_image_base_prefix: Optional[str] = None,
    ):
        self._source = source
        self._storage = storage
        self._embedding = embedding_client or EmbeddingClient()
        self._normalizer = normalizer or ImageNormalizer.from_env()
        configured_prefix = (
            oss_image_base_prefix
            if oss_image_base_prefix is not None
            else os.getenv(
                'OSS_IMAGE_BASE_PREFIX',
                DEFAULT_OSS_IMAGE_BASE_PREFIX,
            )
        )
        self._base_prefix = configured_prefix.strip('/')
        if not self._base_prefix:
            raise ValueError('OSS 图片前缀不能为空')

    def ingest_one(
        self,
        source_relative_path: str,
        *,
        model_number: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> AssetIngestResult:
        if not source_relative_path:
            raise ValueError('来源相对路径不能为空')

        location = self._source.resolve_location()
        source_head = self._source.head_object(source_relative_path)

        try:
            with tempfile.TemporaryDirectory(prefix='image-asset-') as temp_dir:
                source_path = Path(temp_dir) / 'source-image'
                preview_path = Path(temp_dir) / 'search-preview.jpg'

                with source_path.open('w+b') as target:
                    downloaded_size = self._source.download_object(
                        source_relative_path,
                        target,
                    )
                    actual_size = target.tell()

                if (
                    downloaded_size != actual_size
                    or source_head.size != actual_size
                ):
                    raise AssetIngestError(
                        '来源 HEAD、下载返回值与实际字节数不一致'
                    )

                content_hash = self._hash_file(source_path)
                existing = ImageAsset.query.filter_by(
                    source_provider=SOURCE_PROVIDER,
                    source_bucket=location.source_bucket,
                    source_relative_path=source_relative_path,
                    source_revision=1,
                ).one_or_none()
                if existing is not None:
                    if existing.content_hash != content_hash:
                        raise AssetIngestConflictError(
                            '来源冲突：同一来源路径的内容已经变化'
                        )
                    return self._result('existing', existing)

                normalized = self._normalizer.normalize(source_path)
                original_key = self._original_key(
                    location.source_bucket,
                    source_relative_path,
                )
                preview_key = self._preview_key(
                    normalized.normalization_version,
                    content_hash,
                )

                original_metadata = {
                    'source-provider': SOURCE_PROVIDER,
                    'source-bucket': location.source_bucket,
                    'sha256': content_hash,
                    'source-size': str(actual_size),
                }
                self._ensure_file_object(
                    original_key,
                    source_path,
                    expected_size=actual_size,
                    content_type=normalized.source_mime_type,
                    metadata=original_metadata,
                    conflict_name='原图',
                )

                preview_metadata = {
                    'sha256': content_hash,
                    'normalization-version': normalized.normalization_version,
                }
                self._ensure_bytes_object(
                    preview_key,
                    normalized.data,
                    content_type='image/jpeg',
                    metadata=preview_metadata,
                    conflict_name='搜索预览图',
                )

                reusable = ImageAsset.query.filter_by(
                    content_hash=content_hash,
                    embedding_model=EMBEDDING_MODEL,
                    embedding_dimension=EMBEDDING_DIMENSION,
                    normalization_version=normalized.normalization_version,
                ).first()
                if reusable is None:
                    preview_path.write_bytes(normalized.data)
                    vector = self._embedding.embed_image(
                        str(preview_path),
                        request_id=request_id,
                    )
                    vector_values = (
                        vector.tolist()
                        if hasattr(vector, 'tolist')
                        else list(vector)
                    )
                    if len(vector_values) != EMBEDDING_DIMENSION:
                        raise AssetIngestError(
                            'Embedding 返回维度与 image_assets.vector 不一致'
                        )
                else:
                    vector_values = list(reusable.vector)

                asset = ImageAsset(
                    model_number=model_number,
                    source_provider=SOURCE_PROVIDER,
                    source_bucket=location.source_bucket,
                    source_relative_path=source_relative_path,
                    source_revision=1,
                    oss_path=original_key,
                    preview_oss_path=preview_key,
                    content_hash=content_hash,
                    source_size=actual_size,
                    source_mime_type=normalized.source_mime_type,
                    source_width=normalized.source_width,
                    source_height=normalized.source_height,
                    vector=vector_values,
                    embedding_model=EMBEDDING_MODEL,
                    embedding_dimension=EMBEDDING_DIMENSION,
                    normalization_version=normalized.normalization_version,
                    status='active',
                )
                db.session.add(asset)
                db.session.commit()
                return self._result('created', asset)
        except Exception:
            db.session.rollback()
            raise

    def _ensure_file_object(
        self,
        key,
        source_path,
        *,
        expected_size,
        content_type,
        metadata,
        conflict_name,
    ):
        existing = self._storage.head_object(key)
        if existing is not None:
            self._assert_matching(
                existing,
                expected_size=expected_size,
                content_type=content_type,
                metadata=metadata,
                conflict_name=conflict_name,
            )
            return
        self._storage.put_file(
            key,
            source_path,
            content_type=content_type,
            metadata=metadata,
        )
    def _ensure_bytes_object(
        self,
        key,
        data,
        *,
        content_type,
        metadata,
        conflict_name,
    ):
        existing = self._storage.head_object(key)
        if existing is not None:
            self._assert_matching(
                existing,
                expected_size=len(data),
                content_type=content_type,
                metadata=metadata,
                conflict_name=conflict_name,
            )
            return
        self._storage.put_bytes(
            key,
            data,
            content_type=content_type,
            metadata=metadata,
        )

    @staticmethod
    def _assert_matching(
        existing: StoredObject,
        *,
        expected_size: int,
        content_type: str,
        metadata,
        conflict_name: str,
    ) -> None:
        actual_metadata = {
            str(name).lower(): str(value)
            for name, value in existing.metadata.items()
        }
        metadata_matches = all(
            actual_metadata.get(str(name).lower()) == str(value)
            for name, value in metadata.items()
        )
        if (
            existing.size != expected_size
            or existing.content_type != content_type
            or not metadata_matches
        ):
            raise AssetIngestConflictError(
                f'OSS {conflict_name}对象冲突，已存在对象未被覆盖'
            )

    def _original_key(self, source_bucket: str, relative_path: str) -> str:
        # relative_path 不做 normpath/lstrip；完整保留 Kodo Object Key。
        return f'{self._base_prefix}/{source_bucket}/{relative_path}'

    def _preview_key(self, version: str, content_hash: str) -> str:
        return (
            f'{self._base_prefix}/previews/{version}/'
            f'{content_hash[:2]}/{content_hash}.jpg'
        )

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _result(status: str, asset: ImageAsset) -> AssetIngestResult:
        return AssetIngestResult(
            status=status,
            asset_id=str(asset.id),
            content_hash=asset.content_hash,
            oss_path=asset.oss_path,
            preview_oss_path=asset.preview_oss_path,
        )
