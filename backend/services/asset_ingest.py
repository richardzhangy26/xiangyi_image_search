"""来源图片到私有 OSS 与独立图片资产表的统一入库服务。"""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional, Sequence

from sqlalchemy.exc import IntegrityError

from models import ImageAsset, ImageImportItem, db
from services.asset_display_name import default_display_name
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
from services.formal_bucket_identity import FormalBucketIdentityProvider
from services.purge_object_fence import ObjectIdentity, PurgeObjectFenceService
from services.object_binding_fence import BindingFenceLease, ObjectBindingFenceService

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
        asset_id: Optional[str] = None,
        source_relative_path: str = '',
    ):
        self.asset_id = asset_id
        self.source_relative_path = source_relative_path
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
    recovery_action: Optional[dict[str, str]] = None
    # caller-owned（commit=False + control factory）成功时随结果返回的绑定租约；
    # 旧路径恒为 None。调用方回滚外层事务后用 abort_after_outer_rollback 释放。
    binding_lease: object = None


@dataclass(frozen=True)
class ImageImportQueueResult:
    status: str
    item_id: Optional[str]
    asset_id: Optional[str]
    source_relative_path: str
    recovery_action: Optional[dict[str, str]] = None
    # control-factory 模式下随结果返回的绑定租约（commit=False 时供调用方在
    # 外层回滚后 abort_after_outer_rollback）；旧路径恒为 None。
    binding_lease: object = None


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
    source_path: Optional[Path] = None
    location: Optional[SourceLocation] = None
    normalized: Optional[NormalizedImage] = None
    binding_lease: object = None


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
        formal_bucket: Optional[str] = None,
        fence_service: Optional[PurgeObjectFenceService] = None,
        binding_fence_service: Optional[ObjectBindingFenceService] = None,
        control_session_factory: Optional[object] = None,
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
        self._formal_bucket = formal_bucket
        self._fence_service = fence_service
        self._binding_fence_service = binding_fence_service
        self._control_session_factory = control_session_factory
        if formal_bucket is not None and fence_service is None:
            self._fence_service = PurgeObjectFenceService(db.session)
            self._formal_bucket = FormalBucketIdentityProvider(
                formal_bucket
            ).formal_bucket()

    def abort_after_outer_rollback(self, lease) -> bool:
        """Caller invokes after a commit=False outer rollback; legacy path is inert."""
        if self._control_session_factory is None or lease is None:
            return False
        if self._binding_fence_service is None:
            return False
        return self._binding_fence_service.abort_after_rollback(
            lease, control_session_factory=self._control_session_factory,
        )

    def ingest_many_caller_owned(
        self,
        source_relative_paths: Sequence[str],
        *,
        model_number: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> tuple[list[AssetIngestResult], object]:
        """Product 式多图 commit=False 循环的请求级 chunk-owner 入口。

        整请求对完整去重 identity 集一次 ``acquire_prewrite``；逐 item
        ``finalize_in_transaction`` 绑定进调用方事务并只释放独占 original 与
        最后 consumer 的 preview；本方法不 commit。返回 ``(results, lease)``：
        lease 随外层 commit 原子释放，外层回滚后由调用方
        ``abort_after_outer_rollback(lease)`` 释放。请求内任一步失败立即抛出
        （全有或全无，与逐图 ingest_one 语义一致）；finalize 已开始后失败时
        租约挂在异常 ``binding_fence_lease`` 上供边界回滚后回收。
        未注入 control factory 时逐图走旧路径（无租约可交还）。
        输入相对路径须互异（同路径重复会在第二个 item 的 finalize 处确定性
        全有或全无失败；当前所有生产蓝图入口天然产生互异路径）。
        """
        if self._control_session_factory is None:
            results = [
                self.ingest_one(
                    relative_path,
                    model_number=model_number,
                    request_id=request_id,
                    commit=False,
                )
                for relative_path in source_relative_paths
            ]
            return results, None
        if self._binding_fence_service is None:
            raise ValueError('caller-owned 批量入库要求注入绑定围栏服务')

        location = self._source.resolve_location()
        results_by_index: dict[int, AssetIngestResult] = {}
        prepared_items: dict[int, _PreparedAsset] = {}
        lease = None
        finalize_attempted = False
        with tempfile.TemporaryDirectory(prefix='image-assets-caller-owned-') as temp_dir:
            temp_root = Path(temp_dir)
            content_cache: dict[str, _PreparedAsset] = {}
            try:
                for index, relative_path in enumerate(source_relative_paths):
                    if not relative_path:
                        raise ValueError('来源相对路径不能为空')
                    prepared = self._prepare_one(
                        relative_path,
                        location=location,
                        temp_dir=temp_root,
                        item_index=index,
                        model_number=model_number,
                        content_cache=content_cache,
                        stages={},
                    )
                    if isinstance(prepared, AssetIngestResult):
                        results_by_index[index] = prepared
                    else:
                        prepared_items[index] = prepared
                        content_cache.setdefault(prepared.content_hash, prepared)
                if prepared_items:
                    lease = self._binding_fence_service.acquire_prewrite(
                        self._binding_identities(tuple(
                            (prepared.oss_path, prepared.preview_oss_path)
                            for prepared in prepared_items.values()
                        )),
                        owner_kind='asset_ingest',
                        control_session_factory=self._control_session_factory,
                    )
                for prepared in prepared_items.values():
                    self._write_prepared_objects(prepared)
                representatives: list[_PreparedAsset] = []
                vectors_by_hash: dict[str, list[float]] = {}
                for prepared in prepared_items.values():
                    if prepared.vector_values is not None:
                        vectors_by_hash[prepared.content_hash] = prepared.vector_values
                        continue
                    if not any(
                        item.content_hash == prepared.content_hash
                        for item in representatives
                    ):
                        representatives.append(prepared)
                if representatives:
                    vectors = self._embedding.embed_normalized_images(
                        [str(item.preview_path) for item in representatives],
                        request_id=request_id,
                    )
                    for item, vector in zip(representatives, vectors):
                        vectors_by_hash[item.content_hash] = self._vector_values(vector)
                if lease is not None:
                    if not self._binding_fence_service.renew_prewrite(
                        lease,
                        control_session_factory=self._control_session_factory,
                        lease_seconds=300,
                    ):
                        raise AssetIngestError(
                            '绑定围栏租约已失效', stage='database',
                        )
                    preview_remaining: dict[ObjectIdentity, int] = {}
                    for prepared in prepared_items.values():
                        preview_identity = ObjectIdentity(
                            self._formal_bucket, prepared.preview_oss_path,
                        )
                        preview_remaining[preview_identity] = (
                            preview_remaining.get(preview_identity, 0) + 1
                        )
                    first_for_hash: set[str] = set()
                    for index, prepared in prepared_items.items():
                        vector_values = vectors_by_hash.get(prepared.content_hash)
                        if vector_values is None:
                            raise AssetIngestError(
                                'Embedding 未返回可用向量', stage='embedding',
                            )
                        if prepared.stages.get('embedding') != 'reused':
                            prepared.stages['embedding'] = (
                                'new'
                                if prepared.content_hash not in first_for_hash
                                else 'reused'
                            )
                        first_for_hash.add(prepared.content_hash)
                        preview_identity = ObjectIdentity(
                            self._formal_bucket, prepared.preview_oss_path,
                        )
                        preview_remaining[preview_identity] -= 1
                        release_identities = [
                            ObjectIdentity(self._formal_bucket, prepared.oss_path),
                        ]
                        if preview_remaining[preview_identity] == 0:
                            release_identities.append(preview_identity)
                        sublease = ObjectBindingFenceService.sublease(
                            lease, tuple(release_identities),
                        )
                        result_box: dict[str, AssetIngestResult] = {}

                        def _bind():
                            result_box['result'] = self._persist(
                                prepared, vector_values, commit=False,
                            )
                            return True

                        finalize_attempted = True
                        if not self._binding_fence_service.finalize_in_transaction(
                            sublease, db.session, _bind,
                        ):
                            raise AssetIngestError(
                                '绑定围栏租约已失效', stage='database',
                            )
                        results_by_index[index] = result_box['result']
            except Exception as exc:
                if lease is not None:
                    if not finalize_attempted:
                        self.abort_after_outer_rollback(lease)
                    else:
                        exc.binding_fence_lease = lease
                raise
        ordered_results = [
            results_by_index[index]
            for index in range(len(source_relative_paths))
        ]
        return ordered_results, lease

    def queue_many_caller_owned(
        self,
        source_relative_paths: Sequence[str],
        *,
        request_id: Optional[str] = None,
    ) -> tuple[list[ImageImportQueueResult], object]:
        """导入队列多图 commit=False 循环的请求级 chunk-owner 入口。

        与 :meth:`ingest_many_caller_owned` 同协议（不 embedding、绑定
        ``image_import_items``）；未注入 control factory 时逐图走旧路径。
        """
        if self._control_session_factory is None:
            results = [
                self.queue_one(
                    relative_path, request_id=request_id, commit=False,
                )
                for relative_path in source_relative_paths
            ]
            return results, None
        if self._binding_fence_service is None:
            raise ValueError('caller-owned 批量排队要求注入绑定围栏服务')

        location = self._source.resolve_location()
        results_by_index: dict[int, ImageImportQueueResult] = {}
        prepared_items: dict[int, _PreparedAsset] = {}
        lease = None
        finalize_attempted = False
        with tempfile.TemporaryDirectory(prefix='image-import-caller-owned-') as temp_dir:
            temp_root = Path(temp_dir)
            content_cache: dict[str, _PreparedAsset] = {}
            try:
                for index, relative_path in enumerate(source_relative_paths):
                    if not relative_path:
                        raise ValueError('来源相对路径不能为空')
                    prepared = self._prepare_one(
                        relative_path,
                        location=location,
                        temp_dir=temp_root,
                        item_index=index,
                        model_number=None,
                        content_cache=content_cache,
                        stages={},
                    )
                    if isinstance(prepared, AssetIngestResult):
                        results_by_index[index] = ImageImportQueueResult(
                            status=prepared.status,
                            item_id=None,
                            asset_id=prepared.asset_id,
                            source_relative_path=relative_path,
                            recovery_action=prepared.recovery_action,
                        )
                    else:
                        prepared_items[index] = prepared
                        content_cache.setdefault(prepared.content_hash, prepared)
                if prepared_items:
                    lease = self._binding_fence_service.acquire_prewrite(
                        self._binding_identities(tuple(
                            (prepared.oss_path, prepared.preview_oss_path)
                            for prepared in prepared_items.values()
                        )),
                        owner_kind='asset_ingest',
                        control_session_factory=self._control_session_factory,
                    )
                for prepared in prepared_items.values():
                    self._write_prepared_objects(prepared)
                if lease is not None:
                    if not self._binding_fence_service.renew_prewrite(
                        lease,
                        control_session_factory=self._control_session_factory,
                        lease_seconds=300,
                    ):
                        raise AssetIngestError(
                            '绑定围栏租约已失效', stage='database',
                        )
                    preview_remaining: dict[ObjectIdentity, int] = {}
                    for prepared in prepared_items.values():
                        preview_identity = ObjectIdentity(
                            self._formal_bucket, prepared.preview_oss_path,
                        )
                        preview_remaining[preview_identity] = (
                            preview_remaining.get(preview_identity, 0) + 1
                        )
                    request_id_value = request_id or uuid.uuid4().hex
                    for index, prepared in prepared_items.items():
                        preview_identity = ObjectIdentity(
                            self._formal_bucket, prepared.preview_oss_path,
                        )
                        release_identities = [
                            ObjectIdentity(self._formal_bucket, prepared.oss_path),
                        ]
                        preview_remaining[preview_identity] -= 1
                        if preview_remaining[preview_identity] == 0:
                            release_identities.append(preview_identity)
                        sublease = ObjectBindingFenceService.sublease(
                            lease, tuple(release_identities),
                        )
                        result_box: dict[str, ImageImportQueueResult] = {}

                        def _bind():
                            result_box['result'] = self._persist_import_item(
                                prepared,
                                request_id=request_id_value,
                                commit=False,
                            )
                            return True

                        finalize_attempted = True
                        if not self._binding_fence_service.finalize_in_transaction(
                            sublease, db.session, _bind,
                        ):
                            raise AssetIngestError(
                                '绑定围栏租约已失效', stage='database',
                            )
                        results_by_index[index] = result_box['result']
            except Exception as exc:
                if lease is not None:
                    if not finalize_attempted:
                        self.abort_after_outer_rollback(lease)
                    else:
                        exc.binding_fence_lease = lease
                raise
        ordered_results = [
            results_by_index[index]
            for index in range(len(source_relative_paths))
        ]
        return ordered_results, lease

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

        注入 ``control_session_factory`` 且 ``commit=False`` 时走 caller-owned
        分支：围栏经独立 control session 获取与续期，绑定写入调用方事务，
        服务不开启、不提交、不回滚调用方事务；租约随结果返回，外层回滚后
        由调用方 ``abort_after_outer_rollback`` 释放。
        """
        if not source_relative_path:
            raise ValueError('来源相对路径不能为空')

        if self._control_session_factory is not None and not commit:
            return self._ingest_one_caller_owned(
                source_relative_path,
                model_number=model_number,
                request_id=request_id,
            )

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
                self._activate_single_binding_lease(prepared)
                self._write_prepared_objects(prepared)
                if prepared.binding_lease is not None:
                    self._settle_binding_session()
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
                if prepared.binding_lease is not None:
                    if not self._binding_fence_service.renew(
                        prepared.binding_lease, lease_seconds=300,
                    ):
                        raise AssetIngestError('绑定围栏租约已失效', stage='database')
                    result_box = {}
                    if not self._binding_fence_service.final_bind(
                        prepared.binding_lease,
                        bind=lambda: result_box.setdefault(
                            'result', self._persist(prepared, vector_values, commit=False)
                        ),
                    ):
                        raise AssetIngestError('绑定围栏租约已失效', stage='database')
                    return result_box['result']
                return self._persist(prepared, vector_values, commit=commit)
        except Exception:
            if 'prepared' in locals() and prepared.binding_lease is not None:
                self._binding_fence_service.session.rollback()
                self._binding_fence_service.release(prepared.binding_lease, reason='failed')
            if commit:
                db.session.rollback()
            raise

    def _ingest_one_caller_owned(
        self,
        source_relative_path: str,
        *,
        model_number: Optional[str],
        request_id: Optional[str],
    ) -> AssetIngestResult:
        """caller-owned 时序：prepare → acquire_prewrite → 写 OSS → renew → finalize。

        围栏生命周期只经 ``control_session_factory`` 的独立短事务驱动；
        绑定写入 ``db.session`` 的调用方事务（finalize_in_transaction），
        本方法不调用 ``_settle_binding_session``，也不提交或回滚调用方事务。
        """
        if self._binding_fence_service is None:
            raise ValueError('caller-owned 入库要求注入绑定围栏服务')

        location = self._source.resolve_location()
        lease = None
        finalize_attempted = False
        try:
            with tempfile.TemporaryDirectory(prefix='image-asset-') as temp_dir:
                prepared = self._prepare_one(
                    source_relative_path,
                    location=location,
                    temp_dir=Path(temp_dir),
                    item_index=0,
                    model_number=model_number,
                    content_cache={},
                    stages={},
                )
                if isinstance(prepared, AssetIngestResult):
                    return prepared

                self._assert_bindable(prepared.oss_path, prepared.preview_oss_path)
                lease = self._binding_fence_service.acquire_prewrite(
                    self._binding_identities(
                        ((prepared.oss_path, prepared.preview_oss_path),),
                    ),
                    owner_kind='asset_ingest',
                    control_session_factory=self._control_session_factory,
                )
                self._write_prepared_objects(prepared)
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
                if not self._binding_fence_service.renew_prewrite(
                    lease,
                    control_session_factory=self._control_session_factory,
                    lease_seconds=300,
                ):
                    raise AssetIngestError('绑定围栏租约已失效', stage='database')
                result_box: dict[str, AssetIngestResult] = {}

                def _bind() -> bool:
                    result_box['result'] = self._persist(
                        prepared, vector_values, commit=False,
                    )
                    return True

                finalize_attempted = True
                if not self._binding_fence_service.finalize_in_transaction(
                    lease, db.session, _bind,
                ):
                    raise AssetIngestError('绑定围栏租约已失效', stage='database')
                return replace(result_box['result'], binding_lease=lease)
        except Exception as exc:
            # finalize 一旦开始，调用方事务可能已持有围栏行锁；此时服务不越权
            # 释放，把租约挂到异常上由 caller 边界回滚后回收（或租约到期接管）。
            if lease is not None:
                if not finalize_attempted:
                    self.abort_after_outer_rollback(lease)
                else:
                    exc.binding_fence_lease = lease
            raise

    def queue_one(
        self,
        source_relative_path: str,
        *,
        request_id: Optional[str] = None,
        commit: bool = True,
    ) -> ImageImportQueueResult:
        """验证并写入私有对象后持久排队，不在请求内生成 embedding。"""
        if not source_relative_path:
            raise ValueError('来源相对路径不能为空')

        if self._control_session_factory is not None:
            return self._queue_one_with_control_lease(
                source_relative_path,
                request_id=request_id,
                commit=commit,
            )

        location = self._source.resolve_location()
        try:
            with tempfile.TemporaryDirectory(prefix='image-import-') as temp_dir:
                prepared_or_existing = self._prepare_one(
                    source_relative_path,
                    location=location,
                    temp_dir=Path(temp_dir),
                    item_index=0,
                    model_number=None,
                    content_cache={},
                    stages={},
                )
                if isinstance(prepared_or_existing, AssetIngestResult):
                    result = ImageImportQueueResult(
                        status=prepared_or_existing.status,
                        item_id=None,
                        asset_id=prepared_or_existing.asset_id,
                        source_relative_path=source_relative_path,
                        recovery_action=prepared_or_existing.recovery_action,
                    )
                    self._commit_if_requested(commit)
                    return result
                if prepared_or_existing.source_path is not None:
                    self._activate_single_binding_lease(prepared_or_existing)
                    self._write_prepared_objects(prepared_or_existing)
                if prepared_or_existing.binding_lease is not None:
                    self._settle_binding_session()
                    result_box = {}
                    if not self._binding_fence_service.final_bind(
                        prepared_or_existing.binding_lease,
                        bind=lambda: result_box.setdefault(
                            'result', self._persist_import_item(
                                prepared_or_existing,
                                request_id=request_id or uuid.uuid4().hex,
                                commit=False,
                            ),
                        ),
                    ):
                        raise AssetIngestError('绑定围栏租约已失效', stage='database')
                    return result_box['result']
                return self._persist_import_item(
                    prepared_or_existing,
                    request_id=request_id or uuid.uuid4().hex,
                    commit=commit,
                )
        except Exception:
            if 'prepared_or_existing' in locals() and isinstance(prepared_or_existing, _PreparedAsset):
                if prepared_or_existing.binding_lease is not None:
                    self._binding_fence_service.session.rollback()
                    self._binding_fence_service.release(prepared_or_existing.binding_lease, reason='failed')
            if commit:
                db.session.rollback()
            raise

    def _persist_import_item(
        self,
        prepared: _PreparedAsset,
        *,
        request_id: str,
        commit: bool,
    ) -> ImageImportQueueResult:
        existing = self._find_import_item(
            source_bucket=prepared.source_bucket,
            source_relative_path=prepared.source_relative_path,
        )
        if existing is not None:
            result = self._existing_import_result(existing, prepared)
            self._commit_if_requested(commit)
            return result

        item = ImageImportItem(
            source_provider=self._source_provider,
            source_bucket=prepared.source_bucket,
            source_relative_path=prepared.source_relative_path,
            source_revision=1,
            display_name=default_display_name(prepared.source_relative_path),
            oss_path=prepared.oss_path,
            preview_oss_path=prepared.preview_oss_path,
            content_hash=prepared.content_hash,
            source_size=prepared.source_size,
            source_mime_type=prepared.source_mime_type,
            source_width=prepared.source_width,
            source_height=prepared.source_height,
            normalization_version=prepared.normalization_version,
            expected_embedding_model=EMBEDDING_MODEL,
            expected_embedding_dimension=EMBEDDING_DIMENSION,
            status='queued',
            asset_id=None,
            request_id=request_id,
        )
        try:
            with db.session.begin_nested():
                db.session.add(item)
                db.session.flush()
        except IntegrityError as exc:
            winner = self._find_import_item(
                source_bucket=prepared.source_bucket,
                source_relative_path=prepared.source_relative_path,
            )
            if winner is None:
                raise AssetIngestError(
                    '数据库导入项唯一性冲突，但无法读取既有任务',
                    stage='database',
                ) from exc
            result = self._existing_import_result(winner, prepared)
            self._commit_if_requested(commit)
            return result

        self._commit_if_requested(commit)
        return ImageImportQueueResult(
            status='queued',
            item_id=str(item.id),
            asset_id=None,
            source_relative_path=prepared.source_relative_path,
        )

    def _find_import_item(
        self,
        *,
        source_bucket: str,
        source_relative_path: str,
    ):
        return ImageImportItem.query.filter_by(
            source_provider=self._source_provider,
            source_bucket=source_bucket,
            source_relative_path=source_relative_path,
            source_revision=1,
        ).one_or_none()

    @staticmethod
    def _existing_import_result(existing, prepared) -> ImageImportQueueResult:
        if existing.content_hash != prepared.content_hash:
            raise AssetIngestConflictError(
                '来源冲突：同一导入来源身份的内容已经变化',
                stage='database',
                kind='source_conflict',
                source_relative_path=prepared.source_relative_path,
            )
        return ImageImportQueueResult(
            status='existing_task',
            item_id=str(existing.id),
            asset_id=(str(existing.asset_id) if existing.asset_id else None),
            source_relative_path=prepared.source_relative_path,
        )

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
        if self._control_session_factory is not None:
            return self._ingest_batch_with_control_lease(
                source_relative_paths,
                model_number=model_number,
                request_id=request_id,
            )
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

            chunk_lease = self._activate_chunk_binding_lease(
                tuple(prepared_items.values())
            )
            for index, prepared in tuple(prepared_items.items()):
                prepared.binding_lease = chunk_lease
                try:
                    self._write_prepared_objects(prepared)
                except Exception as exc:
                    results[index] = self._failure_result(
                        prepared.source_relative_path, exc, prepared=prepared,
                    )
                    prepared_items.pop(index)

            if chunk_lease is not None:
                self._settle_binding_session()

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
                    result_box = {}
                    if chunk_lease is not None and not self._binding_fence_service.final_bind(
                        chunk_lease,
                        bind=lambda: result_box.setdefault(
                            'result', self._persist(prepared, vector_values, commit=False)
                        ),
                        release=False,
                    ):
                        raise AssetIngestError('绑定围栏租约已失效', stage='database')
                    results[index] = result_box.get('result') or self._persist(
                        prepared, vector_values, commit=True,
                    )
                except Exception as exc:
                    db.session.rollback()
                    results[index] = self._failure_result(
                        prepared.source_relative_path,
                        exc,
                        prepared=prepared,
                    )

            if chunk_lease is not None:
                self._binding_fence_service.release(chunk_lease, reason='completed')

        return [result for result in results if result is not None]

    def _ingest_batch_with_control_lease(
        self,
        source_relative_paths: Sequence[str],
        *,
        model_number: Optional[str],
        request_id: Optional[str],
    ) -> list[AssetIngestResult]:
        """``ingest_many`` 的 chunk-owner control-factory 变体。

        整批一次 ``acquire_prewrite`` 完整去重 identity 集（独立 control session）；
        对象写入与单次 ``embed_normalized_images`` 批量调用保持既有语义；逐 item
        ``finalize_in_transaction`` 在 ``db.session`` 上绑定并提交，只释放该 item 的
        独占 original 与最后一个 consumer 的 preview；批末剩余围栏经 control session
        清扫释放（reason ``failed``），对象为重试保留、不删除。
        """
        if self._binding_fence_service is None:
            raise ValueError('control-factory 批量入库要求注入绑定围栏服务')
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

            chunk_lease = None
            released_fence_ids: set = set()
            try:
                if prepared_items:
                    chunk_lease = self._binding_fence_service.acquire_prewrite(
                        self._binding_identities(tuple(
                            (prepared.oss_path, prepared.preview_oss_path)
                            for prepared in prepared_items.values()
                        )),
                        owner_kind='asset_ingest',
                        control_session_factory=self._control_session_factory,
                    )
                for index, prepared in tuple(prepared_items.items()):
                    try:
                        self._write_prepared_objects(prepared)
                    except Exception as exc:
                        results[index] = self._failure_result(
                            prepared.source_relative_path, exc, prepared=prepared,
                        )
                        prepared_items.pop(index)

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

                if chunk_lease is not None and prepared_items:
                    if not self._binding_fence_service.renew_prewrite(
                        chunk_lease,
                        control_session_factory=self._control_session_factory,
                        lease_seconds=300,
                    ):
                        for index, prepared in prepared_items.items():
                            results[index] = self._failure_result(
                                prepared.source_relative_path,
                                AssetIngestError(
                                    '绑定围栏租约已失效', stage='database',
                                ),
                                prepared=prepared,
                            )
                        prepared_items = {}
                    else:
                        self._bind_chunk_items(
                            chunk_lease,
                            prepared_items,
                            vectors_by_hash,
                            vector_errors,
                            results,
                            released_fence_ids,
                        )
            finally:
                if chunk_lease is not None:
                    remaining = [
                        (identity, fence_id)
                        for identity, fence_id in zip(
                            chunk_lease.identities, chunk_lease.fence_ids,
                        )
                        if fence_id not in released_fence_ids
                    ]
                    if remaining:
                        self.abort_after_outer_rollback(
                            BindingFenceLease(
                                owner_token=chunk_lease.owner_token,
                                owner_generation=chunk_lease.owner_generation,
                                fence_ids=tuple(
                                    fence_id for _, fence_id in remaining
                                ),
                                identities=tuple(
                                    identity for identity, _ in remaining
                                ),
                            ),
                        )

        return [result for result in results if result is not None]

    def _bind_chunk_items(
        self,
        chunk_lease: BindingFenceLease,
        prepared_items: dict[int, _PreparedAsset],
        vectors_by_hash: dict[str, list[float]],
        vector_errors: dict[str, Exception],
        results: list[Optional[AssetIngestResult]],
        released_fence_ids: set,
    ) -> None:
        """chunk 租约下的逐 item 绑定：original 随 item 释放，preview 等最后 consumer。"""
        preview_remaining: dict[ObjectIdentity, int] = {}
        for prepared in prepared_items.values():
            preview_identity = ObjectIdentity(
                self._formal_bucket, prepared.preview_oss_path,
            )
            preview_remaining[preview_identity] = (
                preview_remaining.get(preview_identity, 0) + 1
            )
        first_for_hash: set[str] = set()
        for index, prepared in prepared_items.items():
            preview_identity = ObjectIdentity(
                self._formal_bucket, prepared.preview_oss_path,
            )
            vector_values = vectors_by_hash.get(prepared.content_hash)
            if vector_values is None:
                preview_remaining[preview_identity] -= 1
                results[index] = self._failure_result(
                    prepared.source_relative_path,
                    vector_errors.get(prepared.content_hash) or AssetIngestError(
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
            preview_remaining[preview_identity] -= 1
            release_identities = [
                ObjectIdentity(self._formal_bucket, prepared.oss_path),
            ]
            if preview_remaining[preview_identity] == 0:
                release_identities.append(preview_identity)
            try:
                sublease = ObjectBindingFenceService.sublease(
                    chunk_lease, tuple(release_identities),
                )
                result_box: dict[str, AssetIngestResult] = {}

                def _bind():
                    result_box['result'] = self._persist(
                        prepared, vector_values, commit=False,
                    )
                    return True

                if not self._binding_fence_service.finalize_in_transaction(
                    sublease, db.session, _bind,
                ):
                    raise AssetIngestError(
                        '绑定围栏租约已失效', stage='database',
                    )
                self._commit_if_requested(True)
                released_fence_ids.update(sublease.fence_ids)
                results[index] = result_box['result']
            except Exception as exc:
                db.session.rollback()
                results[index] = self._failure_result(
                    prepared.source_relative_path,
                    exc,
                    prepared=prepared,
                )

    def _queue_one_with_control_lease(
        self,
        source_relative_path: str,
        *,
        request_id: Optional[str],
        commit: bool,
    ) -> ImageImportQueueResult:
        """``queue_one`` 的 control-factory 变体：单 item 租约经独立 control session。"""
        if self._binding_fence_service is None:
            raise ValueError('control-factory 排队入库要求注入绑定围栏服务')
        location = self._source.resolve_location()
        with tempfile.TemporaryDirectory(prefix='image-import-') as temp_dir:
            prepared = self._prepare_one(
                source_relative_path,
                location=location,
                temp_dir=Path(temp_dir),
                item_index=0,
                model_number=None,
                content_cache={},
                stages={},
            )
            if isinstance(prepared, AssetIngestResult):
                result = ImageImportQueueResult(
                    status=prepared.status,
                    item_id=None,
                    asset_id=prepared.asset_id,
                    source_relative_path=source_relative_path,
                    recovery_action=prepared.recovery_action,
                )
                self._commit_if_requested(commit)
                return result

            lease = None
            finalize_attempted = False
            try:
                if prepared.source_path is not None:
                    lease = self._binding_fence_service.acquire_prewrite(
                        self._binding_identities(
                            ((prepared.oss_path, prepared.preview_oss_path),),
                        ),
                        owner_kind='asset_ingest',
                        control_session_factory=self._control_session_factory,
                    )
                    self._write_prepared_objects(prepared)
                if lease is not None and not self._binding_fence_service.renew_prewrite(
                    lease,
                    control_session_factory=self._control_session_factory,
                    lease_seconds=300,
                ):
                    raise AssetIngestError(
                        '绑定围栏租约已失效', stage='database',
                    )
                if lease is None:
                    return self._persist_import_item(
                        prepared,
                        request_id=request_id or uuid.uuid4().hex,
                        commit=commit,
                    )
                result_box: dict[str, ImageImportQueueResult] = {}

                def _bind():
                    result_box['result'] = self._persist_import_item(
                        prepared,
                        request_id=request_id or uuid.uuid4().hex,
                        commit=False,
                    )
                    return True

                finalize_attempted = True
                if not self._binding_fence_service.finalize_in_transaction(
                    lease, db.session, _bind,
                ):
                    raise AssetIngestError(
                        '绑定围栏租约已失效', stage='database',
                    )
                self._commit_if_requested(commit)
                return replace(result_box['result'], binding_lease=lease)
            except Exception as exc:
                if lease is not None:
                    if not finalize_attempted:
                        self.abort_after_outer_rollback(lease)
                    elif commit:
                        db.session.rollback()
                        self.abort_after_outer_rollback(lease)
                    else:
                        exc.binding_fence_lease = lease
                raise

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
        existing = self._find_source_asset(
            source_bucket=location.source_bucket,
            source_relative_path=source_relative_path,
        )
        if existing is not None:
            self._assert_same_source_content(
                existing,
                content_hash=content_hash,
                source_relative_path=source_relative_path,
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
                self._existing_status(existing),
                existing,
                source_relative_path=source_relative_path,
                source_size=actual_size,
                stages=stages,
            )

        cached = content_cache.get(content_hash)
        if cached is not None:
            stages.update({
                'preview': 'reused',
                'embedding': 'reused',
            })
            return _PreparedAsset(
                source_path=source_path,
                location=location,
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
                normalized=None,
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
            stages.update({
                'preview': 'reused',
                'embedding': 'reused',
            })
            return _PreparedAsset(
                source_path=source_path,
                location=location,
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
                normalized=None,
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

        preview_path = temp_dir / f'preview-{item_index}.jpg'
        preview_path.write_bytes(normalized.data)
        return _PreparedAsset(
            source_path=source_path,
            location=location,
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
            normalized=normalized,
            vector_values=None,
            stages=stages,
        )

    def _assert_bindable(self, original_key: str, preview_key: str) -> None:
        if self._fence_service is None:
            return
        self._fence_service.assert_bindable((
            ObjectIdentity(self._formal_bucket, original_key),
            ObjectIdentity(self._formal_bucket, preview_key),
        ))

    def _acquire_binding_lease(self, original_key: str, preview_key: str):
        self._assert_bindable(original_key, preview_key)
        if self._binding_fence_service is None:
            return None
        session = self._binding_fence_service.session
        state_session = session() if callable(session) else session
        if state_session.in_transaction():
            if state_session.new or state_session.dirty or state_session.deleted:
                raise AssetIngestError(
                    '绑定围栏要求在干净事务边界获取', stage='database',
                )
            session.commit()
        return self._binding_fence_service.acquire(
            self._binding_identities(((original_key, preview_key),)),
            owner_kind='asset_ingest', lease_seconds=300,
        )

    def _activate_chunk_binding_lease(self, prepared_items) -> object:
        if self._binding_fence_service is None or not prepared_items:
            return None
        pairs = tuple(
            (prepared.oss_path, prepared.preview_oss_path)
            for prepared in prepared_items
        )
        self._settle_binding_session()
        return self._binding_fence_service.acquire(
            self._binding_identities(pairs),
            owner_kind='asset_ingest', lease_seconds=300,
        )

    def _binding_identities(self, pairs):
        return tuple(
            ObjectIdentity(self._formal_bucket, key)
            for pair in pairs for key in pair
        )

    def _activate_single_binding_lease(self, prepared: _PreparedAsset) -> None:
        prepared.binding_lease = self._acquire_binding_lease(
            prepared.oss_path, prepared.preview_oss_path,
        )

    def _write_prepared_objects(self, prepared: _PreparedAsset) -> None:
        self._assert_bindable(prepared.oss_path, prepared.preview_oss_path)
        if prepared.normalized is not None:
            prepared.stages['original'] = self._ensure_original(
                prepared.oss_path,
                prepared.source_path,
                location=prepared.location,
                content_hash=prepared.content_hash,
                actual_size=prepared.source_size,
                normalized=prepared.normalized,
            )
            prepared.stages['preview'] = self._ensure_preview(
                prepared.preview_oss_path,
                content_hash=prepared.content_hash,
                normalized=prepared.normalized,
            )
            return
        prepared.stages['original'] = self._ensure_original_from_metadata(
            prepared.oss_path,
            prepared.source_path,
            location=prepared.location,
            content_hash=prepared.content_hash,
            actual_size=prepared.source_size,
            source_mime_type=prepared.source_mime_type,
        )
        self._validate_preview_metadata(
            prepared.preview_oss_path,
            content_hash=prepared.content_hash,
            normalization_version=prepared.normalization_version,
        )
        prepared.stages['preview'] = 'reused'

    def _settle_binding_session(self) -> None:
        session = self._binding_fence_service.session
        state_session = session() if callable(session) else session
        if state_session.in_transaction():
            if state_session.new or state_session.dirty or state_session.deleted:
                raise AssetIngestError('绑定围栏后存在未预期数据库写入', stage='database')
            session.commit()

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
            display_name=default_display_name(prepared.source_relative_path),
            version=1,
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
            with db.session.begin_nested():
                db.session.add(asset)
                db.session.flush()
        except IntegrityError as exc:
            winner = self._find_source_asset(
                source_bucket=prepared.source_bucket,
                source_relative_path=prepared.source_relative_path,
            )
            if winner is None:
                if commit:
                    db.session.rollback()
                raise AssetIngestError(
                    '数据库来源身份唯一性冲突，但无法读取既有资产',
                    stage='database',
                ) from exc
            try:
                self._assert_same_source_content(
                    winner,
                    content_hash=prepared.content_hash,
                    source_relative_path=prepared.source_relative_path,
                )
                self._assert_compatible_existing(
                    winner,
                    original_key=prepared.oss_path,
                    preview_key=prepared.preview_oss_path,
                )
            except Exception:
                if commit:
                    db.session.rollback()
                raise
            prepared.stages.update({
                'original': 'reused',
                'preview': 'reused',
                'embedding': 'reused',
                'database': 'reused',
            })
            self._commit_if_requested(commit)
            return self._result(
                self._existing_status(winner),
                winner,
                source_relative_path=prepared.source_relative_path,
                source_size=prepared.source_size,
                stages=prepared.stages,
            )
        except Exception as exc:
            if commit:
                db.session.rollback()
            raise AssetIngestError(
                f'数据库写入失败: {type(exc).__name__}',
                stage='database',
            ) from exc

        self._commit_if_requested(commit)

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
            return self._reuse_after_write_conflict(
                key,
                spec=spec,
                conflict_name=conflict_name,
                cause=exc,
            )
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
            return self._reuse_after_write_conflict(
                key,
                spec=spec,
                conflict_name=conflict_name,
                cause=exc,
            )
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

    def _reuse_after_write_conflict(
        self,
        key: str,
        *,
        spec: ObjectSpec,
        conflict_name: str,
        cause: ObjectStorageConflictError,
    ) -> str:
        if not self._object_needs_upload(key, spec, conflict_name):
            return 'reused'
        raise AssetIngestConflictError(
            f'OSS {conflict_name}对象冲突，已存在对象未被覆盖',
            stage='original' if conflict_name == '原图' else 'preview',
            kind='oss_conflict',
        ) from cause

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

    def _find_source_asset(
        self,
        *,
        source_bucket: str,
        source_relative_path: str,
    ):
        return ImageAsset.query.filter_by(
            source_provider=self._source_provider,
            source_bucket=source_bucket,
            source_relative_path=source_relative_path,
            source_revision=1,
        ).one_or_none()

    @staticmethod
    def _assert_same_source_content(
        existing,
        *,
        content_hash: str,
        source_relative_path: str,
    ) -> None:
        if existing.content_hash != content_hash:
            raise AssetIngestConflictError(
                '来源冲突：同一来源身份的内容已经变化',
                stage='database',
                kind='source_conflict',
                asset_id=str(existing.id),
                source_relative_path=source_relative_path,
            )

    @staticmethod
    def _existing_status(existing) -> str:
        if existing.status == 'active':
            return 'existing'
        if existing.status == 'archived':
            return 'in_recycle_bin'
        raise AssetIngestConflictError(
            '来源记录处于不支持的生命周期状态',
            stage='database',
            kind='version_conflict',
            asset_id=str(existing.id),
            source_relative_path=existing.source_relative_path,
        )

    @staticmethod
    def _commit_if_requested(commit: bool) -> None:
        if not commit:
            return
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            raise AssetIngestError(
                f'数据库提交失败: {type(exc).__name__}',
                stage='database',
            ) from exc

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
        recovery_action = (
            {
                'type': 'open_recycle_bin',
                'asset_id': str(asset.id),
            }
            if status == 'in_recycle_bin'
            else None
        )
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
            recovery_action=recovery_action,
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
            asset_id=(
                exc.asset_id
                if isinstance(exc, AssetIngestConflictError)
                else None
            ),
            content_hash=prepared.content_hash if prepared else None,
            oss_path=prepared.oss_path if prepared else None,
            preview_oss_path=(
                prepared.preview_oss_path if prepared else None
            ),
            source_relative_path=(
                exc.source_relative_path or source_relative_path
                if isinstance(exc, AssetIngestConflictError)
                else source_relative_path
            ),
            source_size=prepared.source_size if prepared else 0,
            stages=completed_stages,
            error_stage=stage,
            error=safe_error,
        )
