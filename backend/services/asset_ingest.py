"""来源图片到私有 OSS 与独立图片资产表的统一入库服务。"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from models import ImageAsset, db
from services.embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    MAX_BATCH_SIZE,
    EmbeddingClient,
    EmbeddingServiceError,
)
from services.image_normalizer import (
    ImageNormalizationError,
    ImageNormalizer,
    NormalizedImage,
)
from services.object_source import ReadOnlyObjectSource, SourceLocation
from services.object_storage import (
    ObjectSpec,
    ObjectStorageConflictError,
    ObjectStorageError,
    ObjectWriter,
    StoredObject,
)

SOURCE_PROVIDER = 'qiniu-kodo'
DEFAULT_OSS_IMAGE_BASE_PREFIX = 'image-search'


class AssetIngestError(RuntimeError):
    """单张图片资产入库失败，携带稳定阶段供 API/报告使用。"""

    def __init__(
        self,
        message: str,
        *,
        stage: str = 'ingest',
        kind: str = 'failed',
    ):
        self.stage = stage
        self.kind = kind
        super().__init__(message)


class AssetIngestConflictError(AssetIngestError):
    """来源或目标对象已存在，但内容不允许安全复用。"""

    def __init__(
        self,
        message: str,
        *,
        stage: str = 'database',
        kind: str = 'source_conflict',
    ):
        super().__init__(message, stage=stage, kind=kind)


@dataclass(frozen=True)
class AssetIngestResult:
    status: str
    asset_id: Optional[str]
    content_hash: Optional[str]
    oss_path: Optional[str]
    preview_oss_path: Optional[str]
    source_relative_path: str = ''
    source_size: int = 0
    stages: dict[str, str] = field(default_factory=dict)
    error_stage: Optional[str] = None
    error: Optional[str] = None


@dataclass
class _PreparedAsset:
    source_relative_path: str
    source_bucket: str
    model_number: Optional[str]
    content_hash: str
    source_size: int
    source_mime_type: str
    source_width: int
    source_height: int
    normalization_version: str
    oss_path: str
    preview_oss_path: str
    preview_path: Optional[Path]
    vector_values: Optional[list[float]]
    stages: dict[str, str]


class ImageAssetIngestService:
    """下载、标准化、无覆盖上传、向量化并持久化图片资产。"""

    def __init__(
        self,
        *,
        source: ReadOnlyObjectSource,
        storage: ObjectWriter,
        embedding_client=None,
        normalizer: Optional[ImageNormalizer] = None,
        oss_image_base_prefix: Optional[str] = None,
        source_provider: str = SOURCE_PROVIDER,
    ):
        self._source = source
        self._storage = storage
        self._embedding = embedding_client or EmbeddingClient()
        self._normalizer = normalizer or ImageNormalizer.from_env()
        self._source_provider = source_provider.strip()
        if not self._source_provider:
            raise ValueError('来源提供方不能为空')

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
        commit: bool = True,
    ) -> AssetIngestResult:
        """入库一张图片。

        ``commit=False`` 时只 flush，事务由调用方统一提交或回滚；Product
        multipart 请求借此保证商品字段和多张图片全有或全无。
        """
        if not source_relative_path:
            raise ValueError('来源相对路径不能为空')

        location = self._source.resolve_location()
        try:
            with tempfile.TemporaryDirectory(prefix='image-asset-') as temp_dir:
                stages: dict[str, str] = {}
                prepared_or_existing = self._prepare_one(
                    source_relative_path,
                    location=location,
                    temp_dir=Path(temp_dir),
                    item_index=0,
                    model_number=model_number,
                    content_cache={},
                    stages=stages,
                )
                if isinstance(prepared_or_existing, AssetIngestResult):
                    return prepared_or_existing

                prepared = prepared_or_existing
                vector_values = prepared.vector_values
                if vector_values is None:
                    try:
                        vector = self._embedding.embed_normalized_image(
                            str(prepared.preview_path),
                            request_id=request_id,
                        )
                    except EmbeddingServiceError:
                        raise
                    except Exception as exc:
                        raise AssetIngestError(
                            f'Embedding 失败: {type(exc).__name__}',
                            stage='embedding',
                        ) from exc
                    vector_values = self._vector_values(vector)
                    prepared.stages['embedding'] = 'new'

                return self._persist(
                    prepared,
                    vector_values,
                    commit=commit,
                )
        except Exception:
            if commit:
                db.session.rollback()
            raise

    def ingest_many(
        self,
        source_relative_paths: Sequence[str],
        *,
        model_number: Optional[str] = None,
        request_id: Optional[str] = None,
        batch_size: int = MAX_BATCH_SIZE,
    ) -> list[AssetIngestResult]:
        """批量入库并隔离单项失败；embedding 批大小严格限制在 1–20。"""
        try:
            batch_size = int(batch_size)
        except (TypeError, ValueError) as exc:
            raise ValueError('batch_size 必须是整数') from exc
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise ValueError(
                f'batch_size 必须在 1 到 {MAX_BATCH_SIZE} 之间'
            )
        if not source_relative_paths:
            return []

        session = db.session()
        if session.new or session.dirty or session.deleted:
            raise AssetIngestError(
                '批量入库要求调用方先结束已有数据库写事务',
                stage='database',
            )

        unique_paths: list[str] = []
        unique_index_by_path: dict[str, int] = {}
        result_indexes: list[int] = []
        for relative_path in source_relative_paths:
            if relative_path not in unique_index_by_path:
                unique_index_by_path[relative_path] = len(unique_paths)
                unique_paths.append(relative_path)
            result_indexes.append(unique_index_by_path[relative_path])

        # batch_size 是端到端工作集上限，而不只是 DashScope 单次请求上限。
        # 这样 full 模式不会先把整个数据集的原图和预览全部堆在临时盘，
        # 每个批次完成后也已经提交到数据库，可安全断点续跑。
        unique_results: list[AssetIngestResult] = []
        for start in range(0, len(unique_paths), batch_size):
            unique_results.extend(self._ingest_batch(
                unique_paths[start:start + batch_size],
                model_number=model_number,
                request_id=request_id,
            ))

        seen_indexes: set[int] = set()
        results: list[AssetIngestResult] = []
        for result_index in result_indexes:
            result = unique_results[result_index]
            if (
                result_index in seen_indexes
                and result.status in {'created', 'existing'}
            ):
                result = AssetIngestResult(
                    status='existing',
                    asset_id=result.asset_id,
                    content_hash=result.content_hash,
                    oss_path=result.oss_path,
                    preview_oss_path=result.preview_oss_path,
                    source_relative_path=result.source_relative_path,
                    source_size=result.source_size,
                    stages={
                        stage: 'reused'
                        for stage in (
                            'download',
                            'original',
                            'preview',
                            'embedding',
                            'database',
                        )
                    },
                )
            results.append(result)
            seen_indexes.add(result_index)
        return results

    def _ingest_batch(
        self,
        source_relative_paths: Sequence[str],
        *,
        model_number: Optional[str],
        request_id: Optional[str],
    ) -> list[AssetIngestResult]:
        """处理一个已限制大小的端到端批次。"""
        location = self._source.resolve_location()
        results: list[Optional[AssetIngestResult]] = [
        None
        ] * len(source_relative_paths)
        prepared_items: dict[int, _PreparedAsset] = {}
        content_cache: dict[str, _PreparedAsset] = {}
        item_stages: list[dict[str, str]] = [
            {} for _ in source_relative_paths
        ]

        with tempfile.TemporaryDirectory(prefix='image-assets-batch-') as temp_dir:
            temp_root = Path(temp_dir)
            for index, relative_path in enumerate(source_relative_paths):
                try:
                    prepared_or_existing = self._prepare_one(
                        relative_path,
                        location=location,
                        temp_dir=temp_root,
                        item_index=index,
                        model_number=model_number,
                        content_cache=content_cache,
                        stages=item_stages[index],
                    )
                    if isinstance(prepared_or_existing, AssetIngestResult):
                        results[index] = prepared_or_existing
                    else:
                        prepared_items[index] = prepared_or_existing
                        content_cache.setdefault(
                            prepared_or_existing.content_hash,
                            prepared_or_existing,
                        )
                except Exception as exc:
                    db.session.rollback()
                    results[index] = self._failure_result(
                        relative_path,
                        exc,
                        stages=item_stages[index],
                    )

            vectors_by_hash: dict[str, list[float]] = {}
            representatives: list[_PreparedAsset] = []
            for prepared in prepared_items.values():
                if prepared.vector_values is not None:
                    vectors_by_hash[prepared.content_hash] = (
                        prepared.vector_values
                    )
                    continue
                if not any(
                    item.content_hash == prepared.content_hash
                    for item in representatives
                ):
                    representatives.append(prepared)

            if representatives:
                try:
                    vectors = self._embedding.embed_normalized_images(
                        [str(item.preview_path) for item in representatives],
                        request_id=request_id,
                    )
                except Exception:
                    vectors = [None] * len(representatives)
            else:
                vectors = []
            vector_errors: dict[str, Exception] = {}
            for prepared, vector in zip(representatives, vectors):
                if vector is None:
                    continue
                try:
                    vectors_by_hash[prepared.content_hash] = (
                        self._vector_values(vector)
                    )
                except Exception as exc:
                    # 一条损坏/错维向量只能让对应内容失败，不能终止同批其余项。
                    vector_errors[prepared.content_hash] = (
                        exc
                        if isinstance(exc, AssetIngestError)
                        else AssetIngestError(
                            f'Embedding 向量格式无效: {type(exc).__name__}',
                            stage='embedding',
                        )
                    )

            first_for_hash: set[str] = set()
            for index, prepared in prepared_items.items():
                vector_values = vectors_by_hash.get(prepared.content_hash)
                if vector_values is None:
                    vector_error = vector_errors.get(prepared.content_hash)
                    results[index] = self._failure_result(
                        prepared.source_relative_path,
                        vector_error or AssetIngestError(
                            'Embedding 未返回可用向量',
                            stage='embedding',
                        ),
                        prepared=prepared,
                    )
                    continue

                if prepared.stages.get('embedding') != 'reused':
                    prepared.stages['embedding'] = (
                        'new'
                        if prepared.content_hash not in first_for_hash
                        else 'reused'
                    )
                first_for_hash.add(prepared.content_hash)

                try:
                    results[index] = self._persist(
                        prepared,
                        vector_values,
                        commit=True,
                    )
                except Exception as exc:
                    db.session.rollback()
                    results[index] = self._failure_result(
                        prepared.source_relative_path,
                        exc,
                        prepared=prepared,
                    )

        return [result for result in results if result is not None]

    def _prepare_one(
        self,
        source_relative_path: str,
        *,
        location: SourceLocation,
        temp_dir: Path,
        item_index: int,
        model_number: Optional[str],
        content_cache: dict[str, _PreparedAsset],
        stages: dict[str, str],
    ):
        if not source_relative_path:
            raise ValueError('来源相对路径不能为空')

        source_path = temp_dir / f'source-{item_index}'
        try:
            source_head = self._source.head_object(source_relative_path)
            with source_path.open('w+b') as target:
                downloaded_size = self._source.download_object(
                    source_relative_path,
                    target,
                )
                actual_size = target.tell()
        except Exception as exc:
            raise AssetIngestError(
                f'来源下载失败: {type(exc).__name__}',
                stage='download',
            ) from exc
        if (
            downloaded_size != actual_size
            or source_head.size != actual_size
        ):
            raise AssetIngestError(
                '来源 HEAD、下载返回值与实际字节数不一致',
                stage='download',
            )
        stages['download'] = 'new'

        content_hash = self._hash_file(source_path)
        original_key = self._original_key(
            location.source_bucket,
            source_relative_path,
        )
        preview_key = self._preview_key(
            self._normalizer.normalization_version,
            content_hash,
        )
        existing = ImageAsset.query.filter_by(
            source_provider=self._source_provider,
            source_bucket=location.source_bucket,
            source_relative_path=source_relative_path,
            source_revision=1,
        ).one_or_none()
        if existing is not None:
            if existing.content_hash != content_hash:
                raise AssetIngestConflictError(
                    '来源冲突：同一来源路径的内容已经变化',
                    stage='database',
                    kind='source_conflict',
                )
            self._assert_compatible_existing(
                existing,
                original_key=original_key,
                preview_key=preview_key,
            )
            self._validate_existing_asset_objects(
                existing,
                source_path=source_path,
                actual_size=actual_size,
                source_bucket=location.source_bucket,
                stages=stages,
            )
            stages.update({
                'embedding': 'reused',
                'database': 'reused',
            })
            return self._result(
                'existing',
                existing,
                source_relative_path=source_relative_path,
                source_size=actual_size,
                stages=stages,
            )

        cached = content_cache.get(content_hash)
        if cached is not None:
            original_status = self._ensure_original_from_metadata(
                original_key,
                source_path,
                location=location,
                content_hash=content_hash,
                actual_size=actual_size,
                source_mime_type=cached.source_mime_type,
            )
            stages.update({
                'original': original_status,
                'preview': 'reused',
                'embedding': 'reused',
            })
            return _PreparedAsset(
                source_relative_path=source_relative_path,
                source_bucket=location.source_bucket,
                model_number=model_number,
                content_hash=content_hash,
                source_size=actual_size,
                source_mime_type=cached.source_mime_type,
                source_width=cached.source_width,
                source_height=cached.source_height,
                normalization_version=cached.normalization_version,
                oss_path=original_key,
                preview_oss_path=cached.preview_oss_path,
                preview_path=cached.preview_path,
                vector_values=cached.vector_values,
                stages=stages,
            )

        reusable = ImageAsset.query.filter_by(
            content_hash=content_hash,
            embedding_model=EMBEDDING_MODEL,
            embedding_dimension=EMBEDDING_DIMENSION,
            normalization_version=self._normalizer.normalization_version,
        ).first()
        if reusable is not None:
            if reusable.preview_oss_path != preview_key:
                raise AssetIngestConflictError(
                    '兼容内容记录的预览对象布局冲突',
                    stage='preview',
                    kind='oss_conflict',
                )
            original_status = self._ensure_original_from_metadata(
                original_key,
                source_path,
                location=location,
                content_hash=content_hash,
                actual_size=actual_size,
                source_mime_type=reusable.source_mime_type,
            )
            stages['original'] = original_status
            self._validate_preview_metadata(
                preview_key,
                content_hash=content_hash,
                normalization_version=reusable.normalization_version,
            )
            stages.update({
                'preview': 'reused',
                'embedding': 'reused',
            })
            return _PreparedAsset(
                source_relative_path=source_relative_path,
                source_bucket=location.source_bucket,
                model_number=model_number,
                content_hash=content_hash,
                source_size=actual_size,
                source_mime_type=reusable.source_mime_type,
                source_width=reusable.source_width,
                source_height=reusable.source_height,
                normalization_version=reusable.normalization_version,
                oss_path=original_key,
                preview_oss_path=preview_key,
                preview_path=None,
                vector_values=list(reusable.vector),
                stages=stages,
            )

        try:
            normalized = self._normalizer.normalize(source_path)
        except ImageNormalizationError:
            raise
        except Exception as exc:
            raise AssetIngestError(
                f'搜索预览图生成失败: {type(exc).__name__}',
                stage='preview',
            ) from exc

        original_status = self._ensure_original(
            original_key,
            source_path,
            location=location,
            content_hash=content_hash,
            actual_size=actual_size,
            normalized=normalized,
        )
        stages['original'] = original_status
        preview_status = self._ensure_preview(
            preview_key,
            content_hash=content_hash,
            normalized=normalized,
        )
        stages['preview'] = preview_status
        preview_path = temp_dir / f'preview-{item_index}.jpg'
        preview_path.write_bytes(normalized.data)
        return _PreparedAsset(
            source_relative_path=source_relative_path,
            source_bucket=location.source_bucket,
            model_number=model_number,
            content_hash=content_hash,
            source_size=actual_size,
            source_mime_type=normalized.source_mime_type,
            source_width=normalized.source_width,
            source_height=normalized.source_height,
            normalization_version=normalized.normalization_version,
            oss_path=original_key,
            preview_oss_path=preview_key,
            preview_path=preview_path,
            vector_values=None,
            stages=stages,
        )

    def _persist(
        self,
        prepared: _PreparedAsset,
        vector_values: list[float],
        *,
        commit: bool,
    ) -> AssetIngestResult:
        if len(vector_values) != EMBEDDING_DIMENSION:
            raise AssetIngestError(
                'Embedding 返回维度与 image_assets.vector 不一致',
                stage='embedding',
            )
        asset = ImageAsset(
            model_number=prepared.model_number,
            source_provider=self._source_provider,
            source_bucket=prepared.source_bucket,
            source_relative_path=prepared.source_relative_path,
            source_revision=1,
            oss_path=prepared.oss_path,
            preview_oss_path=prepared.preview_oss_path,
            content_hash=prepared.content_hash,
            source_size=prepared.source_size,
            source_mime_type=prepared.source_mime_type,
            source_width=prepared.source_width,
            source_height=prepared.source_height,
            vector=vector_values,
            embedding_model=EMBEDDING_MODEL,
            embedding_dimension=EMBEDDING_DIMENSION,
            normalization_version=prepared.normalization_version,
            status='active',
        )
        try:
            db.session.add(asset)
            db.session.flush()
            if commit:
                db.session.commit()
        except Exception as exc:
            if commit:
                db.session.rollback()
            raise AssetIngestError(
                f'数据库写入失败: {type(exc).__name__}',
                stage='database',
            ) from exc

        prepared.stages['database'] = 'new'
        return self._result(
            'created',
            asset,
            source_relative_path=prepared.source_relative_path,
            source_size=prepared.source_size,
            stages=prepared.stages,
        )

    def _ensure_original(
        self,
        key,
        source_path,
        *,
        location,
        content_hash,
        actual_size,
        normalized,
    ):
        return self._ensure_original_from_metadata(
            key,
            source_path,
            location=location,
            content_hash=content_hash,
            actual_size=actual_size,
            source_mime_type=normalized.source_mime_type,
        )

    def _ensure_original_from_metadata(
        self,
        key,
        source_path,
        *,
        location,
        content_hash,
        actual_size,
        source_mime_type,
    ):
        spec = ObjectSpec(
            size=actual_size,
            content_type=source_mime_type,
            metadata={
                'source-provider': self._source_provider,
                'source-bucket': location.source_bucket,
                'sha256': content_hash,
                'source-size': str(actual_size),
            },
            md5_hex=self._md5_file(source_path),
        )
        return self._ensure_file_object(
            key,
            source_path,
            spec=spec,
            conflict_name='原图',
        )

    def _ensure_preview(
        self,
        key,
        *,
        content_hash,
        normalized,
    ):
        preview_md5 = self._md5_bytes(normalized.data)
        spec = ObjectSpec(
            size=len(normalized.data),
            content_type='image/jpeg',
            metadata={
                'sha256': content_hash,
                'normalization-version': normalized.normalization_version,
                'preview-md5': preview_md5,
                'preview-size': str(len(normalized.data)),
            },
            md5_hex=preview_md5,
        )
        return self._ensure_bytes_object(
            key,
            normalized.data,
            spec=spec,
            conflict_name='搜索预览图',
        )

    def _ensure_file_object(
        self,
        key,
        source_path,
        *,
        spec,
        conflict_name,
    ):
        if not self._object_needs_upload(key, spec, conflict_name):
            return 'reused'
        try:
            self._storage.put_file(key, source_path, spec=spec)
        except ObjectStorageConflictError as exc:
            raise AssetIngestConflictError(
                f'OSS {conflict_name}对象冲突，已存在对象未被覆盖',
                stage='original',
                kind='oss_conflict',
            ) from exc
        except ObjectStorageError as exc:
            raise AssetIngestError(
                f'OSS {conflict_name}上传失败',
                stage='original',
            ) from exc
        return 'new'

    def _ensure_bytes_object(
        self,
        key,
        data,
        *,
        spec,
        conflict_name,
    ):
        if not self._object_needs_upload(key, spec, conflict_name):
            return 'reused'
        try:
            self._storage.put_bytes(key, data, spec=spec)
        except ObjectStorageConflictError as exc:
            raise AssetIngestConflictError(
                f'OSS {conflict_name}对象冲突，已存在对象未被覆盖',
                stage='preview',
                kind='oss_conflict',
            ) from exc
        except ObjectStorageError as exc:
            raise AssetIngestError(
                f'OSS {conflict_name}上传失败',
                stage='preview',
            ) from exc
        return 'new'

    def _object_needs_upload(
        self,
        key: str,
        spec: ObjectSpec,
        conflict_name: str,
    ) -> bool:
        try:
            existing = self._storage.head_object(key)
        except ObjectStorageError as exc:
            raise AssetIngestError(
                f'OSS {conflict_name} HEAD 失败',
                stage='original' if conflict_name == '原图' else 'preview',
            ) from exc
        except Exception as exc:
            raise AssetIngestError(
                f'OSS HEAD 失败: {type(exc).__name__}',
                stage='original' if conflict_name == '原图' else 'preview',
            ) from exc
        if existing is None:
            return True
        self._assert_matching(
            existing,
            spec=spec,
            conflict_name=conflict_name,
        )
        return False

    @staticmethod
    def _assert_matching(
        existing: StoredObject,
        *,
        spec: ObjectSpec,
        conflict_name: str,
    ) -> None:
        actual_metadata = {
            str(name).lower(): str(value)
            for name, value in existing.metadata.items()
        }
        metadata_matches = all(
            actual_metadata.get(str(name).lower()) == str(value)
            for name, value in spec.metadata.items()
        )
        actual_etag = (existing.etag or '').strip('"').lower()
        if (
            existing.size != spec.size
            or existing.content_type != spec.content_type
            or not metadata_matches
            or actual_etag != spec.md5_hex
        ):
            raise AssetIngestConflictError(
                f'OSS {conflict_name}对象冲突，已存在对象未被覆盖',
                stage='original' if conflict_name == '原图' else 'preview',
                kind='oss_conflict',
            )

    def _assert_compatible_existing(
        self,
        existing,
        *,
        original_key,
        preview_key,
    ):
        if (
            existing.oss_path != original_key
            or existing.preview_oss_path != preview_key
            or existing.embedding_model != EMBEDDING_MODEL
            or existing.embedding_dimension != EMBEDDING_DIMENSION
            or existing.normalization_version
            != self._normalizer.normalization_version
        ):
            raise AssetIngestConflictError(
                '来源记录与当前 OSS 对象布局或标准化版本冲突',
                stage='database',
                kind='version_conflict',
            )

    def _validate_existing_asset_objects(
        self,
        existing,
        *,
        source_path,
        actual_size,
        source_bucket,
        stages,
    ):
        original_spec = ObjectSpec(
            size=actual_size,
            content_type=existing.source_mime_type,
            metadata={
                'source-provider': self._source_provider,
                'source-bucket': source_bucket,
                'sha256': existing.content_hash,
                'source-size': str(actual_size),
            },
            md5_hex=self._md5_file(source_path),
        )
        try:
            original = self._storage.head_object(existing.oss_path)
        except ObjectStorageError as exc:
            raise AssetIngestError(
                'OSS 原图 HEAD 失败',
                stage='original',
            ) from exc
        if original is None:
            raise AssetIngestConflictError(
                'OSS 原图对象缺失，未自动覆盖',
                stage='original',
                kind='oss_conflict',
            )
        self._assert_matching(
            original,
            spec=original_spec,
            conflict_name='原图',
        )
        stages['original'] = 'reused'
        self._validate_preview_metadata(
            existing.preview_oss_path,
            content_hash=existing.content_hash,
            normalization_version=existing.normalization_version,
        )
        stages['preview'] = 'reused'

    def _validate_preview_metadata(
        self,
        key,
        *,
        content_hash,
        normalization_version,
    ):
        try:
            preview = self._storage.head_object(key)
        except ObjectStorageError as exc:
            raise AssetIngestError(
                'OSS 搜索预览图 HEAD 失败',
                stage='preview',
            ) from exc
        metadata = (
            {
                str(name).lower(): str(value)
                for name, value in preview.metadata.items()
            }
            if preview is not None
            else {}
        )
        actual_etag = (
            (preview.etag or '').strip('"').lower()
            if preview is not None
            else ''
        )
        if (
            preview is None
            or preview.size <= 0
            or preview.content_type != 'image/jpeg'
            or metadata.get('sha256') != content_hash
            or metadata.get('normalization-version')
            != normalization_version
            or metadata.get('preview-size') != str(preview.size)
            or not metadata.get('preview-md5')
            or metadata.get('preview-md5', '').lower() != actual_etag
        ):
            raise AssetIngestConflictError(
                'OSS 搜索预览图对象冲突，已存在对象未被覆盖',
                stage='preview',
                kind='oss_conflict',
            )

    def _original_key(self, source_bucket: str, relative_path: str) -> str:
        # relative_path 不做 normpath/lstrip；完整保留来源 Object Key。
        if self._source_provider == SOURCE_PROVIDER:
            # 保持首批 Kodo 迁移已经确定的稳定 Key 布局。
            return f'{self._base_prefix}/{source_bucket}/{relative_path}'
        return (
            f'{self._base_prefix}/sources/{self._source_provider}/'
            f'{source_bucket}/{relative_path}'
        )

    def _preview_key(self, version: str, content_hash: str) -> str:
        return (
            f'{self._base_prefix}/previews/{version}/'
            f'{content_hash[:2]}/{content_hash}.jpg'
        )

    @staticmethod
    def _vector_values(vector) -> list[float]:
        values = (
            vector.tolist()
            if hasattr(vector, 'tolist')
            else list(vector)
        )
        if len(values) != EMBEDDING_DIMENSION:
            raise AssetIngestError(
                'Embedding 返回维度与 image_assets.vector 不一致',
                stage='embedding',
            )
        return [float(value) for value in values]

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _md5_file(path: Path) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _md5_bytes(data: bytes) -> str:
        return hashlib.md5(data, usedforsecurity=False).hexdigest()

    @staticmethod
    def _result(
        status: str,
        asset: ImageAsset,
        *,
        source_relative_path: str = '',
        source_size: int = 0,
        stages: Optional[dict[str, str]] = None,
    ) -> AssetIngestResult:
        return AssetIngestResult(
            status=status,
            asset_id=str(asset.id),
            content_hash=asset.content_hash,
            oss_path=asset.oss_path,
            preview_oss_path=asset.preview_oss_path,
            source_relative_path=(
                source_relative_path or asset.source_relative_path
            ),
            source_size=source_size or asset.source_size,
            stages=dict(stages or {}),
        )

    @staticmethod
    def _failure_result(
        source_relative_path: str,
        exc: Exception,
        *,
        prepared: Optional[_PreparedAsset] = None,
        stages: Optional[dict[str, str]] = None,
    ) -> AssetIngestResult:
        if isinstance(exc, AssetIngestError):
            stage = exc.stage
            status = exc.kind
            safe_error = str(exc)
        elif isinstance(exc, ImageNormalizationError):
            stage = 'preview'
            status = 'failed'
            safe_error = str(exc)
        elif isinstance(exc, EmbeddingServiceError):
            stage = 'embedding'
            status = 'failed'
            safe_error = str(exc)
        elif isinstance(exc, ObjectStorageError):
            stage = 'original'
            status = 'failed'
            safe_error = f'对象存储失败: {type(exc).__name__}'
        else:
            stage = 'ingest'
            status = 'failed'
            safe_error = type(exc).__name__
        completed_stages = dict(stages or {})
        if prepared is not None:
            completed_stages.update(prepared.stages)
        return AssetIngestResult(
            status=status,
            asset_id=None,
            content_hash=prepared.content_hash if prepared else None,
            oss_path=prepared.oss_path if prepared else None,
            preview_oss_path=(
                prepared.preview_oss_path if prepared else None
            ),
            source_relative_path=source_relative_path,
            source_size=prepared.source_size if prepared else 0,
            stages=completed_stages,
            error_stage=stage,
            error=safe_error,
        )
