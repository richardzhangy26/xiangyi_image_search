"""永久清除批次的 Flask 安全控制服务。

本模块只管理数据库状态和脱敏审计记录；不得导入备份、对象存储、Kodo 或 ops 环境。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import re
from collections.abc import Sequence
import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError


IDEMPOTENCY_KEY_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$')
_CANCELLABLE_STATUSES = {
    'queued', 'database_backup', 'object_backup', 'verifying', 'pending_deletion', 'failed',
}


class PurgeBatchError(ValueError):
    error_code = 'PURGE_BATCH_INVALID'

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        if error_code is not None:
            self.error_code = error_code


class IdempotencyKeyError(PurgeBatchError):
    error_code = 'INVALID_PURGE_IDEMPOTENCY_KEY'


class IdempotencyConflictError(PurgeBatchError):
    error_code = 'PURGE_IDEMPOTENCY_CONFLICT'


class PurgeBatchStateError(PurgeBatchError):
    pass


@dataclass(frozen=True)
class CreateResult:
    batch: object
    replayed: bool


@dataclass(frozen=True)
class ClaimedPurgeBatch:
    batch_id: uuid.UUID
    claim_token: uuid.UUID
    claim_generation: int
    expected_status: str


def canonical_fingerprint(asset_ids: Sequence[uuid.UUID], confirmation: str) -> str:
    if len(asset_ids) != len(set(asset_ids)):
        raise PurgeBatchError('图片资产 ID 不能重复', error_code='DUPLICATE_PURGE_ASSET_ID')
    payload = {
        'asset_ids': sorted(str(value) for value in asset_ids),
        'confirmation': confirmation,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def _parse_asset_ids(asset_ids: Sequence[uuid.UUID | str]) -> list[uuid.UUID]:
    if not isinstance(asset_ids, (list, tuple)) or not 1 <= len(asset_ids) <= 20:
        raise PurgeBatchError('永久清除批次必须包含 1 至 20 张图片', error_code='INVALID_PURGE_ASSET_SELECTION')
    try:
        parsed = [value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)) for value in asset_ids]
    except (TypeError, ValueError, AttributeError):
        raise PurgeBatchError('图片资产 ID 无效', error_code='INVALID_PURGE_ASSET_SELECTION') from None
    if len(parsed) != len(set(parsed)):
        raise PurgeBatchError('图片资产 ID 不能重复', error_code='DUPLICATE_PURGE_ASSET_ID')
    return parsed


def _confirmation_for(count: int) -> str:
    return f'永久删除 {count} 张'


class PurgeBatchControlService:
    """所有 HTTP 批次写入的事务边界；不拥有任何外部副作用。"""

    def __init__(self, session, *, clock=datetime.now):
        self.session = session
        self.clock = clock

    def create_or_replay(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        asset_ids: Sequence[uuid.UUID | str],
        confirmation: str,
        request_id: str,
    ) -> CreateResult:
        from models import ImageAsset, PurgeBatch, PurgeBatchItem

        if not isinstance(idempotency_key, str) or IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key) is None:
            raise IdempotencyKeyError('幂等键格式无效')
        parsed_ids = _parse_asset_ids(asset_ids)
        if confirmation != _confirmation_for(len(parsed_ids)):
            raise PurgeBatchError('确认文字不匹配', error_code='INVALID_PURGE_CONFIRMATION')
        fingerprint = canonical_fingerprint(parsed_ids, confirmation)

        existing = self._existing(actor_id, idempotency_key)
        if existing is not None:
            return self._replay_or_conflict(existing, fingerprint)

        lock_ids = sorted(parsed_ids, key=str)
        try:
            assets = self.session.execute(
                select(ImageAsset)
                .where(ImageAsset.id.in_(lock_ids))
                .order_by(ImageAsset.id)
                .with_for_update()
            ).scalars().all()
            by_id = {asset.id: asset for asset in assets}
            if len(by_id) != len(lock_ids):
                raise PurgeBatchStateError('图片资产不存在', error_code='PURGE_ASSET_NOT_FOUND')
            if any(by_id[asset_id].status != 'archived' for asset_id in lock_ids):
                raise PurgeBatchStateError('只能清除回收站图片', error_code='PURGE_ASSET_NOT_ARCHIVED')

            held = self.session.execute(
                select(PurgeBatchItem.target_asset_id)
                .join(PurgeBatch, PurgeBatch.id == PurgeBatchItem.batch_id)
                .where(PurgeBatchItem.target_asset_id.in_(lock_ids))
                .where(PurgeBatch.status != 'cancelled')
                .limit(1)
            ).scalar_one_or_none()
            if held is not None:
                raise PurgeBatchStateError('图片已属于未取消的清除批次', error_code='PURGE_ASSET_IN_ACTIVE_BATCH')

            # The actor/key unique constraint can race a concurrent request.
            # Keep the failed insert contained so the winner can be reread.
            with self.session.begin_nested():
                batch = PurgeBatch(
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_fingerprint_sha256=fingerprint,
                    confirmation_text=confirmation,
                    status='queued',
                )
                self.session.add(batch)
                self.session.flush()
                self.session.add_all([
                    PurgeBatchItem(
                        batch_id=batch.id,
                        target_asset_id=asset_id,
                        ordinal=index,
                    )
                    for index, asset_id in enumerate(lock_ids)
                ])
            self._record(
                'purge.batch.created', batch, request_id, result='succeeded'
            )
            self.session.commit()
            return CreateResult(batch=batch, replayed=False)
        except IntegrityError:
            existing = self._existing(actor_id, idempotency_key)
            if existing is not None:
                return self._replay_or_conflict(existing, fingerprint)
            self.session.rollback()
            raise

    def advance_verified_to_pending_if_current(self, batch_id, *, authorizations, manifest_sha256):
        """Only complete verified item authorizations may enter pending_deletion."""
        from models import ImageAsset, PurgeBatch, PurgeBatchItem

        try:
            batch = self.session.execute(
                select(PurgeBatch).where(PurgeBatch.id == batch_id, PurgeBatch.status == 'verifying').with_for_update()
            ).scalar_one_or_none()
            if batch is None or batch.object_manifest_sha256 != manifest_sha256:
                raise ValueError('verified batch/manifest mismatch')
            items = self.session.execute(
                select(PurgeBatchItem).where(PurgeBatchItem.batch_id == batch.id).order_by(PurgeBatchItem.ordinal).with_for_update()
            ).scalars().all()
            if set(authorizations) != {item.target_asset_id for item in items}:
                raise ValueError('incomplete item authorization')
            for item in items:
                asset = self.session.execute(
                    select(ImageAsset).where(ImageAsset.id == item.target_asset_id, ImageAsset.status == 'archived').with_for_update()
                ).scalar_one_or_none()
                auth = authorizations[item.target_asset_id]
                required = ('original_formal_key', 'original_backup_object_id', 'original_backup_sha256', 'preview_formal_key', 'authorization_retain_until')
                if asset is None or any(not auth.get(field) for field in required):
                    raise ValueError('incomplete item authorization')
                if asset.oss_path != auth['original_formal_key'] or asset.preview_oss_path != auth['preview_formal_key']:
                    raise ValueError('asset identity mismatch')
                for field, value in auth.items():
                    if hasattr(item, field):
                        setattr(item, field, value)
                item.status = 'pending'
                item.checkpoint = 'pending'
            batch.status = 'pending_deletion'
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise
        except Exception:
            self.session.rollback()
            raise

    def cancel(self, batch_id, *, actor_id: str, request_id: str):
        batch = self._locked_batch(batch_id, actor_id)
        if batch.status not in _CANCELLABLE_STATUSES or (batch.status == 'pending_deletion' and batch.deleting_at is not None):
            self.session.rollback()
            raise PurgeBatchStateError('批次当前不可取消', error_code='PURGE_BATCH_NOT_CANCELLABLE')
        batch.status = 'cancelled'
        batch.cancelled_at = self.clock()
        batch.claim_generation += 1
        batch.claim_token = None
        batch.claimed_by = None
        batch.lease_expires_at = None
        self._record('purge.batch.cancelled', batch, request_id, result='succeeded')
        self.session.commit()
        return batch

    def retry(self, batch_id, *, actor_id: str, request_id: str):
        batch = self._locked_batch(batch_id, actor_id)
        if batch.status != 'failed' or batch.error_code == 'PURGE_BACKUP_RETENTION_EXPIRED':
            self.session.rollback()
            raise PurgeBatchStateError('批次当前不可重试', error_code='PURGE_BATCH_NOT_RETRYABLE')
        batch.status = 'queued'
        batch.error_code = None
        batch.failed_at = None
        batch.claim_generation += 1
        batch.claim_token = None
        batch.claimed_by = None
        batch.lease_expires_at = None
        self._record('purge.batch.retried', batch, request_id, result='succeeded')
        self.session.commit()
        return batch

    def get_batch(self, batch_id, *, actor_id: str):
        return self._locked_batch(batch_id, actor_id, lock=False)

    def list_batches(self, *, actor_id: str, limit: int = 20, cursor=None):
        from models import PurgeBatch
        from sqlalchemy.orm import selectinload

        bounded = min(max(int(limit), 1), 100)
        statement = (
            select(PurgeBatch)
            .options(selectinload(PurgeBatch.items))
            .where(PurgeBatch.actor_id == actor_id)
        )
        if cursor:
            try:
                parsed = uuid.UUID(str(cursor))
            except (TypeError, ValueError, AttributeError):
                raise PurgeBatchStateError('清除批次标识无效', error_code='INVALID_PURGE_BATCH_ID') from None
            current = self.session.execute(
                select(PurgeBatch).where(
                    PurgeBatch.id == parsed, PurgeBatch.actor_id == actor_id,
                )
            ).scalar_one_or_none()
            if current is not None:
                statement = statement.where(
                    or_(
                        PurgeBatch.created_at < current.created_at,
                        and_(
                            PurgeBatch.created_at == current.created_at,
                            PurgeBatch.id < current.id,
                        ),
                    )
                )
        return self.session.execute(
            statement.order_by(PurgeBatch.created_at.desc(), PurgeBatch.id.desc()).limit(bounded)
        ).scalars().all()

    def claim_next(self, *, worker_id: str, lease_seconds: int, now=None):
        """领取一个可执行批次，并在同一事务将 queued 声明为首个阶段。"""
        from models import PurgeBatch

        now = now or self.clock()
        if not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError('lease_seconds 必须为正整数')
        in_progress = ('database_backup', 'object_backup', 'verifying')
        statement = (
            select(PurgeBatch)
            .where(or_(
                PurgeBatch.status == 'queued',
                and_(
                    PurgeBatch.status.in_(in_progress),
                    PurgeBatch.lease_expires_at.is_not(None),
                    PurgeBatch.lease_expires_at <= now,
                ),
            ))
            .order_by(PurgeBatch.created_at, PurgeBatch.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        try:
            batch = self.session.execute(statement).scalar_one_or_none()
            if batch is None:
                self.session.rollback()
                return None
            expected_status = batch.status
            if batch.status == 'queued':
                batch.status = 'database_backup'
                batch.started_at = batch.started_at or now
            batch.claim_generation += 1
            batch.claim_token = uuid.uuid4()
            batch.claimed_by = worker_id[:128]
            batch.lease_expires_at = now + timedelta(seconds=lease_seconds)
            self._record('purge.batch.claimed', batch, 'worker', result='succeeded')
            self.session.commit()
            return ClaimedPurgeBatch(
                batch_id=batch.id,
                claim_token=batch.claim_token,
                claim_generation=batch.claim_generation,
                expected_status='database_backup' if expected_status == 'queued' else expected_status,
            )
        except Exception:
            self.session.rollback()
            raise

    def item_asset_ids(self, batch_id):
        from models import PurgeBatchItem
        try:
            rows = self.session.execute(
                select(PurgeBatchItem.target_asset_id)
                .where(PurgeBatchItem.batch_id == batch_id)
                .order_by(PurgeBatchItem.ordinal, PurgeBatchItem.target_asset_id)
            ).scalars().all()
            return tuple(rows)
        finally:
            # 该只读 helper 与快照 reader 共享 session；不得遗留 autobegin。
            self.session.rollback()

    def record_stale_result(self, claim: ClaimedPurgeBatch, *, request_id: str = 'worker'):
        from models import AssetActivityRecord

        self.session.add(AssetActivityRecord(
            event_type='purge.batch.stale_result',
            target_type='purge_batch',
            target_id=str(claim.batch_id),
            batch_id=str(claim.batch_id),
            request_id=request_id[:64],
            source='worker',
            result='ignored',
            after_state={'status': claim.expected_status},
        ))
        self.session.commit()

    def advance_if_current(
        self,
        claim: ClaimedPurgeBatch,
        *,
        status: str,
        now=None,
        database_backup_id=None,
        database_manifest_sha256=None,
        object_manifest_sha256=None,
        retain_until=None,
    ):
        """仅当前 token/generation/阶段可推进；迟到结果静默失效。"""
        from models import PurgeBatch

        now = now or self.clock()
        batch = self.session.execute(
            select(PurgeBatch).where(
                PurgeBatch.id == claim.batch_id,
                PurgeBatch.claim_token == claim.claim_token,
                PurgeBatch.claim_generation == claim.claim_generation,
                PurgeBatch.status == claim.expected_status,
            ).with_for_update()
        ).scalar_one_or_none()
        if batch is None:
            self.session.rollback()
            return False
        batch.status = status
        batch.lease_expires_at = now
        if database_backup_id:
            batch.database_backup_id = database_backup_id
        if database_manifest_sha256:
            batch.database_manifest_sha256 = database_manifest_sha256
        if object_manifest_sha256:
            batch.object_manifest_sha256 = object_manifest_sha256
        if retain_until is not None:
            batch.retain_until = retain_until
        if status == 'pending_deletion':
            batch.completed_at = now
        self._record(f'purge.batch.{claim.expected_status}.succeeded', batch, 'worker', result='succeeded')
        self.session.commit()
        return True

    def fail_if_current(self, claim: ClaimedPurgeBatch, *, error_code: str, retryable: bool, now=None):
        from models import PurgeBatch

        now = now or self.clock()
        batch = self.session.execute(
            select(PurgeBatch).where(
                PurgeBatch.id == claim.batch_id,
                PurgeBatch.claim_token == claim.claim_token,
                PurgeBatch.claim_generation == claim.claim_generation,
                PurgeBatch.status == claim.expected_status,
            ).with_for_update()
        ).scalar_one_or_none()
        if batch is None:
            self.session.rollback()
            return False
        batch.status = 'failed'
        batch.error_code = error_code
        batch.failed_at = now
        batch.claim_token = None
        batch.claimed_by = None
        batch.lease_expires_at = None
        self._record('purge.batch.failed', batch, 'worker', result='failed')
        self.session.commit()
        return True

    def _existing(self, actor_id: str, idempotency_key: str):
        from models import PurgeBatch

        return self.session.execute(
            select(PurgeBatch)
            .where(PurgeBatch.actor_id == actor_id, PurgeBatch.idempotency_key == idempotency_key)
        ).scalar_one_or_none()

    @staticmethod
    def _replay_or_conflict(batch, fingerprint: str) -> CreateResult:
        if batch.request_fingerprint_sha256 != fingerprint:
            raise IdempotencyConflictError('幂等键已用于不同请求')
        return CreateResult(batch=batch, replayed=True)

    def _locked_batch(self, batch_id, actor_id: str, *, lock: bool = True):
        from models import PurgeBatch

        try:
            parsed = batch_id if isinstance(batch_id, uuid.UUID) else uuid.UUID(str(batch_id))
        except (TypeError, ValueError, AttributeError):
            raise PurgeBatchStateError('清除批次标识无效', error_code='INVALID_PURGE_BATCH_ID') from None
        statement = select(PurgeBatch).where(
            PurgeBatch.id == parsed, PurgeBatch.actor_id == actor_id
        )
        if lock:
            statement = statement.with_for_update()
        batch = self.session.execute(statement).scalar_one_or_none()
        if batch is None:
            self.session.rollback()
            raise PurgeBatchStateError('清除批次不存在', error_code='PURGE_BATCH_NOT_FOUND')
        return batch

    def _record(self, event_type: str, batch, request_id: str, *, result: str):
        from models import AssetActivityRecord

        self.session.add(AssetActivityRecord(
            event_type=event_type,
            target_type='purge_batch',
            target_id=str(batch.id),
            batch_id=str(batch.id),
            request_id=request_id[:64],
            source='api',
            actor_id=batch.actor_id,
            result=result,
            error_code=batch.error_code,
            after_state={'status': batch.status, 'error_code': batch.error_code},
        ))
