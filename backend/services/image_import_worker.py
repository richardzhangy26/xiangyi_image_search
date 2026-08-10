"""PostgreSQL 支撑的持久图片导入 worker。"""

from __future__ import annotations

import logging
import math
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, Sequence

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from models import AssetActivityRecord, ImageAsset, ImageImportItem
from services import import_retry
from services import import_retention
from services.embedding import (
    EMBEDDING_DIMENSION,
    EMBEDDING_MODEL,
    EmbeddingResult,
)


logger = logging.getLogger(__name__)
DEFAULT_LEASE_SECONDS = 300


class InvalidEmbeddingResult(ValueError):
    """向量结果不满足正式资产的强约束。"""


class ImportPromotionConflict(RuntimeError):
    """任务元数据无法与并发写入的正式资产安全合并。"""


@dataclass(frozen=True)
class ClaimedImportItem:
    item_id: uuid.UUID
    claim_token: uuid.UUID
    claim_generation: int
    attempt_count: int
    request_id: str
    source_provider: str
    source_bucket: str
    source_relative_path: str
    source_revision: int
    display_name: str
    oss_path: str
    preview_oss_path: str
    content_hash: str
    source_size: int
    source_mime_type: str
    source_width: int
    source_height: int
    normalization_version: str
    expected_embedding_model: str
    expected_embedding_dimension: int
    created_at: datetime


def _claim_snapshot(item: ImageImportItem) -> ClaimedImportItem:
    return ClaimedImportItem(
        item_id=item.id,
        claim_token=item.claim_token,
        claim_generation=item.claim_generation,
        attempt_count=item.attempt_count,
        request_id=item.request_id,
        source_provider=item.source_provider,
        source_bucket=item.source_bucket,
        source_relative_path=item.source_relative_path,
        source_revision=item.source_revision,
        display_name=item.display_name,
        oss_path=item.oss_path,
        preview_oss_path=item.preview_oss_path,
        content_hash=item.content_hash,
        source_size=item.source_size,
        source_mime_type=item.source_mime_type,
        source_width=item.source_width,
        source_height=item.source_height,
        normalization_version=item.normalization_version,
        expected_embedding_model=item.expected_embedding_model,
        expected_embedding_dimension=item.expected_embedding_dimension,
        created_at=item.created_at,
    )


def claim_next_import_item(
    session,
    *,
    worker_id: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    now: datetime | None = None,
) -> ClaimedImportItem | None:
    """以 SKIP LOCKED 领取最早任务，并在返回前提交领取状态。"""
    now = now or datetime.now()
    lease_expires_at = now + timedelta(seconds=lease_seconds)
    try:
        item = session.execute(
            select(ImageImportItem)
            .where(
                ImageImportItem.asset_id.is_(None),
                ImageImportItem.cancel_requested_at.is_(None),
                or_(
                    ImageImportItem.status == 'queued',
                    and_(
                        ImageImportItem.status == 'embedding',
                        ImageImportItem.lease_expires_at < now,
                    ),
                    and_(
                        ImageImportItem.status == 'awaiting_retry',
                        ImageImportItem.next_retry_at <= now,
                    ),
                ),
            )
            .order_by(ImageImportItem.created_at, ImageImportItem.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        ).scalar_one_or_none()
        if item is None:
            session.rollback()
            return None

        item.status = 'embedding'
        item.claim_token = uuid.uuid4()
        item.claim_generation += 1
        item.attempt_count += 1
        item.last_attempt_at = now
        item.next_retry_at = None
        item.claimed_by = worker_id[:128]
        item.claimed_at = now
        item.lease_expires_at = lease_expires_at
        item.embedding_started_at = now
        item.updated_at = now
        claim = _claim_snapshot(item)
        session.commit()
        return claim
    except Exception:
        session.rollback()
        raise


def validate_embedding_result(
    result: EmbeddingResult,
    *,
    claim: ClaimedImportItem,
) -> list[float]:
    """拒绝异模型、错维、非数值与非有限向量。"""
    if (
        result.model != EMBEDDING_MODEL
        or result.model != claim.expected_embedding_model
    ):
        raise InvalidEmbeddingResult('embedding 模型不匹配')
    try:
        values = [float(value) for value in result.vector]
    except (TypeError, ValueError, OverflowError) as exc:
        raise InvalidEmbeddingResult('embedding 向量包含非数值') from exc
    if (
        len(values) != EMBEDDING_DIMENSION
        or len(values) != claim.expected_embedding_dimension
    ):
        raise InvalidEmbeddingResult('embedding 向量维度不匹配')
    if not all(math.isfinite(value) for value in values):
        raise InvalidEmbeddingResult('embedding 向量包含非有限值')
    return values


def _source_identity_clause(item):
    return (
        ImageAsset.source_provider == item.source_provider,
        ImageAsset.source_bucket == item.source_bucket,
        ImageAsset.source_relative_path == item.source_relative_path,
        ImageAsset.source_revision == item.source_revision,
    )


def _assert_compatible_asset(asset, item) -> None:
    if asset.content_hash != item.content_hash:
        raise ImportPromotionConflict('同一来源身份存在不同内容')
    if (
        asset.oss_path != item.oss_path
        or asset.preview_oss_path != item.preview_oss_path
        or asset.embedding_model != EMBEDDING_MODEL
        or asset.embedding_dimension != EMBEDDING_DIMENSION
        or asset.normalization_version != item.normalization_version
        or asset.status not in {'active', 'archived'}
    ):
        raise ImportPromotionConflict('正式资产与导入任务不兼容')


def _find_source_asset(session, item):
    return session.execute(
        select(ImageAsset)
        .where(*_source_identity_clause(item))
        .with_for_update()
    ).scalar_one_or_none()


def _new_asset(item, vector: Sequence[float]) -> ImageAsset:
    return ImageAsset(
        model_number=None,
        source_provider=item.source_provider,
        source_bucket=item.source_bucket,
        source_relative_path=item.source_relative_path,
        source_revision=item.source_revision,
        display_name=item.display_name,
        version=1,
        oss_path=item.oss_path,
        preview_oss_path=item.preview_oss_path,
        content_hash=item.content_hash,
        source_size=item.source_size,
        source_mime_type=item.source_mime_type,
        source_width=item.source_width,
        source_height=item.source_height,
        vector=list(vector),
        embedding_model=EMBEDDING_MODEL,
        embedding_dimension=EMBEDDING_DIMENSION,
        normalization_version=item.normalization_version,
        status='active',
    )


def _transition_item_to_cancelled(session, item, *, event_type, now):
    """把已锁定的任务行转入 cancelled 终态并写入活动记录（不提交）。"""
    item.status = 'cancelled'
    item.cancelled_at = now
    # Issue #22：取消项进入保留窗口，到期后由清理任务处理暂存对象。
    item.purge_eligible_at = import_retention.cancel_purge_deadline(now)
    item.claim_token = None
    item.claimed_by = None
    item.claimed_at = None
    item.lease_expires_at = None
    item.next_retry_at = None
    item.updated_at = now
    session.add(AssetActivityRecord(
        event_type=event_type,
        target_type='image_import_item',
        target_id=str(item.id),
        task_id=str(item.id),
        request_id=item.request_id[:64],
        source='worker',
        before_state={
            'status': 'embedding',
            'claim_generation': item.claim_generation,
        },
        after_state={'status': 'cancelled'},
        result='cancelled',
    ))


def cancel_import_item_if_requested(
    session,
    claim: ClaimedImportItem,
    *,
    now: datetime | None = None,
) -> bool:
    """调用 embedding 前的检查点：若已提交取消意图则转入 cancelled。"""
    now = now or datetime.now()
    try:
        item = session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == claim.item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            item is None
            or item.status != 'embedding'
            or item.claim_token != claim.claim_token
            or item.cancel_requested_at is None
        ):
            session.rollback()
            return False
        _transition_item_to_cancelled(
            session, item, event_type='image_import.cancelled', now=now
        )
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise


def sweep_cancelled_imports(session, *, now: datetime | None = None) -> int:
    """清扫已提交取消意图却无人收敛的行，防止僵尸行。

    覆盖 queued/failed/awaiting_retry（无人处理）与租约过期的 embedding
    （worker 崩溃）。仍被有效租约持有的 embedding 行留给 worker 检查点处理。
    """
    now = now or datetime.now()
    try:
        rows = session.execute(
            select(ImageImportItem)
            .where(
                ImageImportItem.cancel_requested_at.is_not(None),
                ImageImportItem.asset_id.is_(None),
                ImageImportItem.status != 'cancelled',
                or_(
                    ImageImportItem.status.in_(
                        ('queued', 'failed', 'awaiting_retry')
                    ),
                    and_(
                        ImageImportItem.status == 'embedding',
                        ImageImportItem.lease_expires_at < now,
                    ),
                ),
            )
            .with_for_update(skip_locked=True)
        ).scalars().all()
        for item in rows:
            _transition_item_to_cancelled(
                session, item, event_type='image_import.cancelled', now=now
            )
        session.commit()
        return len(rows)
    except Exception:
        session.rollback()
        raise


def complete_import_item(
    session,
    claim: ClaimedImportItem,
    vector: Sequence[float],
    *,
    now: datetime | None = None,
) -> bool | str:
    """在一个事务内建立正式资产并完成仍由本 claim 拥有的任务。

    若提交前发现取消意图，则丢弃迟到结果并转入 cancelled，返回 'discarded'，
    绝不创建正式资产；所有权失效返回 False；成功返回 True。
    """
    now = now or datetime.now()
    try:
        item = session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == claim.item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            item is None
            or item.status != 'embedding'
            or item.claim_token != claim.claim_token
        ):
            session.rollback()
            return False

        if item.cancel_requested_at is not None:
            _transition_item_to_cancelled(
                session,
                item,
                event_type='image_import.late_result_discarded',
                now=now,
            )
            session.commit()
            return 'discarded'

        checked_vector = validate_embedding_result(
            EmbeddingResult(model=EMBEDDING_MODEL, vector=vector),
            claim=claim,
        )
        asset = _find_source_asset(session, item)
        if asset is None:
            candidate = _new_asset(item, checked_vector)
            try:
                with session.begin_nested():
                    session.add(candidate)
                    session.flush()
                asset = candidate
            except IntegrityError as exc:
                asset = _find_source_asset(session, item)
                if asset is None:
                    raise ImportPromotionConflict(
                        '正式资产唯一性冲突后无法读取胜出记录'
                    ) from exc
        _assert_compatible_asset(asset, item)

        item.asset_id = asset.id
        item.status = 'completed'
        item.completed_at = now
        item.failed_at = None
        item.failure_message = None
        item.claim_token = None
        item.claimed_by = None
        item.claimed_at = None
        item.lease_expires_at = None
        item.updated_at = now
        session.add(AssetActivityRecord(
            event_type='image_import.completed',
            target_type='image_import_item',
            target_id=str(item.id),
            task_id=str(item.id),
            request_id=item.request_id[:64],
            source='worker',
            before_state={
                'status': 'embedding',
                'claim_generation': claim.claim_generation,
            },
            after_state={
                'status': 'completed',
                'asset_id': str(asset.id),
            },
            result='completed',
        ))
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise


def mark_import_item_failed(
    session,
    claim: ClaimedImportItem,
    failure_message: str,
    *,
    error_class: str | None = None,
    now: datetime | None = None,
) -> bool:
    """回滚处理事务后，在独立短事务内标记仍由本 claim 拥有的任务失败。"""
    now = now or datetime.now()
    try:
        item = session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == claim.item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            item is None
            or item.status != 'embedding'
            or item.claim_token != claim.claim_token
        ):
            session.rollback()
            return False
        # 取消意图优先：处理失败但已被请求取消时转入 cancelled 而非 failed。
        if item.cancel_requested_at is not None:
            _transition_item_to_cancelled(
                session, item, event_type='image_import.cancelled', now=now
            )
            session.commit()
            return True
        item.status = 'failed'
        item.failed_at = now
        item.failure_message = failure_message[:512]
        item.last_error_class = error_class
        item.last_attempt_at = now
        # Issue #22：重试耗尽的失败项进入保留窗口，手工重试会重新计算。
        item.purge_eligible_at = import_retention.failed_purge_deadline(now)
        item.claim_token = None
        item.claimed_by = None
        item.claimed_at = None
        item.lease_expires_at = None
        item.updated_at = now
        session.add(AssetActivityRecord(
            event_type='image_import.failed',
            target_type='image_import_item',
            target_id=str(item.id),
            task_id=str(item.id),
            request_id=item.request_id[:64],
            source='worker',
            before_state={
                'status': 'embedding',
                'claim_generation': claim.claim_generation,
            },
            after_state={
                'status': 'failed',
                'error_class': error_class,
            },
            result='failed',
        ))
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise


def schedule_import_retry(
    session,
    claim: ClaimedImportItem,
    *,
    error_class: str,
    failure_message: str,
    now: datetime | None = None,
) -> bool:
    """把仍由本 claim 拥有的失败任务持久转入等待重试；等待由领取过滤表达。"""
    now = now or datetime.now()
    try:
        item = session.execute(
            select(ImageImportItem)
            .where(ImageImportItem.id == claim.item_id)
            .with_for_update()
        ).scalar_one_or_none()
        if (
            item is None
            or item.status != 'embedding'
            or item.claim_token != claim.claim_token
        ):
            session.rollback()
            return False
        # 取消意图优先于重试调度：已请求取消的任务不得再次进入等待重试。
        if item.cancel_requested_at is not None:
            _transition_item_to_cancelled(
                session, item, event_type='image_import.cancelled', now=now
            )
            session.commit()
            return True
        delay_seconds = import_retry.next_retry_delay_seconds(
            claim.attempt_count
        )
        item.status = 'awaiting_retry'
        item.next_retry_at = now + timedelta(seconds=delay_seconds)
        item.last_error_class = error_class
        item.last_attempt_at = now
        item.failure_message = failure_message[:512]
        item.failed_at = None
        item.claim_token = None
        item.claimed_by = None
        item.claimed_at = None
        item.lease_expires_at = None
        item.updated_at = now
        session.add(AssetActivityRecord(
            event_type='image_import.awaiting_retry',
            target_type='image_import_item',
            target_id=str(item.id),
            task_id=str(item.id),
            request_id=item.request_id[:64],
            source='worker',
            before_state={
                'status': 'embedding',
                'claim_generation': claim.claim_generation,
            },
            after_state={
                'status': 'awaiting_retry',
                'error_class': error_class,
                'attempt_count': claim.attempt_count,
                'next_retry_at': item.next_retry_at.isoformat(),
            },
            result='awaiting_retry',
        ))
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise


class ImportRepository(Protocol):
    def claim_next(self, *, worker_id: str, lease_seconds: int): ...
    def complete(self, claim: ClaimedImportItem, vector: Sequence[float]): ...
    def fail(
        self,
        claim: ClaimedImportItem,
        failure_message: str,
        *,
        error_class: str | None = None,
    ): ...
    def schedule_retry(
        self,
        claim: ClaimedImportItem,
        *,
        error_class: str,
        failure_message: str,
    ): ...
    def cancel_if_requested(self, claim: ClaimedImportItem): ...
    def sweep_cancelled(self) -> int: ...
    def queue_depth(self) -> int: ...


class SqlAlchemyImageImportRepository:
    """把 worker 流程适配到一个 SQLAlchemy session。"""

    def __init__(self, session):
        self._session = session

    def claim_next(self, *, worker_id: str, lease_seconds: int):
        return claim_next_import_item(
            self._session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
        )

    def complete(self, claim, vector):
        return complete_import_item(self._session, claim, vector)

    def fail(self, claim, failure_message, *, error_class=None):
        return mark_import_item_failed(
            self._session,
            claim,
            failure_message,
            error_class=error_class,
        )

    def schedule_retry(self, claim, *, error_class, failure_message):
        return schedule_import_retry(
            self._session,
            claim,
            error_class=error_class,
            failure_message=failure_message,
        )

    def cancel_if_requested(self, claim):
        return cancel_import_item_if_requested(self._session, claim)

    def sweep_cancelled(self) -> int:
        return sweep_cancelled_imports(self._session)

    def queue_depth(self) -> int:
        try:
            count = self._session.execute(
                select(func.count(ImageImportItem.id)).where(
                    ImageImportItem.status.in_(('queued', 'embedding'))
                )
            ).scalar_one()
            return int(count)
        finally:
            self._session.rollback()


class ImageImportWorker:
    """一次只处理一个持久任务；进程退出后可由租约恢复。"""

    def __init__(
        self,
        *,
        repository: ImportRepository,
        storage,
        embedding_client,
        worker_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ):
        self._repository = repository
        self._storage = storage
        self._embedding = embedding_client
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds

    def process_one(self) -> bool:
        total_started = time.perf_counter()
        claim = self._repository.claim_next(
            worker_id=self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if claim is None:
            # 空闲时清扫已提交取消意图却无人收敛的行（崩溃恢复）。
            self._repository.sweep_cancelled()
            return False

        # 检查点 1：调用 embedding 前确认没有已提交的取消意图。
        if self._repository.cancel_if_requested(claim):
            logger.info(
                'image_import.cancelled_before_embedding task_id=%s '
                'worker_id=%s claim_generation=%s',
                claim.item_id,
                self._worker_id,
                claim.claim_generation,
            )
            return True

        embedding_duration_ms = 0
        queue_latency_ms = max(
            0,
            int((datetime.now() - claim.created_at).total_seconds() * 1000),
        )
        try:
            with tempfile.TemporaryDirectory(prefix='image-import-worker-') as temp_dir:
                preview_path = Path(temp_dir) / 'preview.jpg'
                self._storage.download_file(
                    claim.preview_oss_path,
                    preview_path,
                )
                embedding_started = time.perf_counter()
                result = self._embedding.embed_normalized_image_result(
                    str(preview_path),
                    request_id=claim.request_id,
                )
                embedding_duration_ms = int(
                    (time.perf_counter() - embedding_started) * 1000
                )
                vector = validate_embedding_result(result, claim=claim)
                completed = self._repository.complete(claim, vector)
                if completed == 'discarded':
                    logger.info(
                        'image_import.late_result_discarded task_id=%s '
                        'worker_id=%s claim_generation=%s',
                        claim.item_id,
                        self._worker_id,
                        claim.claim_generation,
                    )
                    return True
                if not completed:
                    logger.warning(
                        'image_import.stale task_id=%s worker_id=%s '
                        'claim_generation=%s',
                        claim.item_id,
                        self._worker_id,
                        claim.claim_generation,
                    )
                    return True
        except Exception as exc:
            failure_message = f'处理失败（{type(exc).__name__}）'
            error_class = import_retry.classify_import_failure(exc)
            try:
                if import_retry.should_auto_retry(
                    error_class, claim.attempt_count
                ):
                    self._repository.schedule_retry(
                        claim,
                        error_class=error_class,
                        failure_message=failure_message,
                    )
                else:
                    self._repository.fail(
                        claim,
                        failure_message,
                        error_class=error_class,
                    )
            except Exception as state_exc:
                logger.error(
                    'image_import.failure_state_write_failed task_id=%s '
                    'worker_id=%s claim_generation=%s error_type=%s',
                    claim.item_id,
                    self._worker_id,
                    claim.claim_generation,
                    type(state_exc).__name__,
                )
            logger.error(
                'image_import.failed task_id=%s worker_id=%s '
                'claim_generation=%s error_type=%s error_class=%s',
                claim.item_id,
                self._worker_id,
                claim.claim_generation,
                type(exc).__name__,
                error_class,
            )
        finally:
            queue_depth = self._queue_depth_for_log(claim)
            logger.info(
                'image_import.processed task_id=%s worker_id=%s '
                'claim_generation=%s queue_depth=%s queue_latency_ms=%s '
                'embedding_duration_ms=%s total_duration_ms=%s',
                claim.item_id,
                self._worker_id,
                claim.claim_generation,
                queue_depth,
                queue_latency_ms,
                embedding_duration_ms,
                int((time.perf_counter() - total_started) * 1000),
            )
        return True

    def _queue_depth_for_log(self, claim: ClaimedImportItem) -> int:
        try:
            return self._repository.queue_depth()
        except Exception as exc:
            logger.error(
                'image_import.observation_failed task_id=%s worker_id=%s '
                'claim_generation=%s error_type=%s',
                claim.item_id,
                self._worker_id,
                claim.claim_generation,
                type(exc).__name__,
            )
            return -1

    def process_until_idle(self, max_items: int | None = None) -> int:
        processed = 0
        while max_items is None or processed < max_items:
            if not self.process_one():
                break
            processed += 1
        return processed
