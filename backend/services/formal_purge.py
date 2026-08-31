"""Checkpoint orchestration seam for #27; T13 production capability is off."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, text


class CanonicalFormalPurgeAuthorizationVerifier:
    """Fail-closed verifier seam for canonical manifest/copy/retention evidence."""

    def __init__(self, *, manifest_loader, verify_copies, clock=datetime.now):
        self._manifest_loader = manifest_loader
        self._verify_copies = verify_copies
        self._clock = clock

    def verify_for_operation(self, batch, item, stage):
        try:
            if batch.retain_until is None or item.authorization_retain_until is None:
                return False
            if batch.retain_until <= self._clock() or item.authorization_retain_until <= self._clock():
                return False
            manifest = self._manifest_loader(batch)
            if not isinstance(manifest, dict) or manifest.get('sha256') != batch.object_manifest_sha256:
                return False
            if str(manifest.get('batch_id')) != str(batch.id):
                return False
            if not self._verify_copies(manifest):
                return False
            authorized = {
                (str(entry.get('asset_id')), entry.get('kind')): entry
                for entry in manifest.get('items', []) if isinstance(entry, dict)
            }
            original = authorized.get((str(item.target_asset_id), 'source_image'))
            preview = authorized.get((str(item.target_asset_id), 'search_preview'))
            if not original or original.get('formal_key') != item.original_formal_key:
                return False
            if original.get('backup_object_id') != item.original_backup_object_id or original.get('sha256') != item.original_backup_sha256:
                return False
            if item.preview_delete_authorized:
                return bool(preview and preview.get('formal_key') == item.preview_formal_key and preview.get('backup_object_id') == item.preview_backup_object_id and preview.get('sha256') == item.preview_backup_sha256)
            return True
        except Exception:
            return False


@dataclass(frozen=True)
class ClaimedFormalPurgeItem:
    batch_id: uuid.UUID
    target_asset_id: uuid.UUID
    claim_token: uuid.UUID
    claim_generation: int
    original_key: str
    preview_key: str
    preview_delete_authorized: bool
    checkpoint: str


class FormalPurgeRepository:
    """PostgreSQL item claims; authorization snapshots are mandatory."""

    def __init__(self, session, *, clock=datetime.now, manifest_validator=None):
        self.session = session
        self.clock = clock
        if manifest_validator is None:
            raise ValueError('canonical manifest verifier is required')
        self.manifest_validator = manifest_validator

    def claim_next_item(self, *, worker_id='formal-purge', lease_seconds=60):
        from models import ImageAsset, PurgeBatch, PurgeBatchItem

        now = self.session.execute(text('SELECT clock_timestamp()::timestamp')).scalar_one()
        try:
            item = self.session.execute(
                select(PurgeBatchItem)
                .join(PurgeBatch, PurgeBatch.id == PurgeBatchItem.batch_id)
                .join(ImageAsset, ImageAsset.id == PurgeBatchItem.target_asset_id)
                .where(
                    PurgeBatch.status.in_(('pending_deletion', 'deleting')),
                    PurgeBatch.retain_until > now,
                    or_(
                        PurgeBatchItem.status.in_(('pending', 'failed')),
                        and_(PurgeBatchItem.status == 'in_progress', PurgeBatchItem.lease_expires_at <= now),
                    ),
                    PurgeBatchItem.authorization_retain_until > now,
                    PurgeBatchItem.original_formal_key.is_not(None),
                    PurgeBatchItem.original_backup_object_id.is_not(None),
                    PurgeBatchItem.original_backup_sha256.is_not(None),
                    PurgeBatchItem.preview_formal_key.is_not(None),
                    ImageAsset.status == 'archived',
                )
                .order_by(PurgeBatch.created_at, PurgeBatchItem.ordinal)
                .with_for_update(skip_locked=True)
                .limit(1)
            ).scalar_one_or_none()
            if item is None:
                self.session.rollback()
                return None
            batch = self.session.execute(
                select(PurgeBatch).where(PurgeBatch.id == item.batch_id).with_for_update()
            ).scalar_one()
            if not self.manifest_validator(batch, item):
                item.status = 'failed'
                item.error_code = 'PURGE_BACKUP_REVALIDATION_FAILED'
                item.failed_at = now
                self._event(item, 'purge.item.failed', now, error_code=item.error_code)
                self._reduce_batch(item.batch_id, now)
                self.session.commit()
                return None
            item.status = 'in_progress'
            item.claim_generation += 1
            item.claim_token = uuid.uuid4()
            item.lease_expires_at = now + timedelta(seconds=lease_seconds)
            item.attempt_count += 1
            item.checkpoint_at = now
            self._event(item, 'purge.item.claimed', now)
            self.session.commit()
            return ClaimedFormalPurgeItem(
                batch_id=item.batch_id,
                target_asset_id=item.target_asset_id,
                claim_token=item.claim_token,
                claim_generation=item.claim_generation,
                original_key=item.original_formal_key,
                preview_key=item.preview_formal_key,
                preview_delete_authorized=item.preview_delete_authorized,
                checkpoint=item.checkpoint,
            )
        except Exception:
            self.session.rollback()
            raise

    def authorize_delete_call(self, claim, expected_checkpoint, verified_authorization, operation_kind):
        """Issue a deleter token only after complete-set fence/CAS authorization."""
        if claim is None or verified_authorization is None:
            return None
        # 声明的授权围栏集必须先通过完整性校验（原图+预览两把、互异且非空），
        # 该拒绝发生在任何 session 事务操作之前；缺省声明时由事务内派生。
        declared_fence_ids = getattr(verified_authorization, 'fence_ids', None)
        if declared_fence_ids is not None and len({
            str(fence_id) for fence_id in declared_fence_ids if fence_id
        }) != 2:
            return None
        try:
            outcome = self._authorize_delete_call_locked(
                claim, expected_checkpoint, operation_kind,
            )
        except Exception:
            self.session.rollback()
            return None
        if outcome is None:
            # 拒绝路径同样结束事务，及时归还完整集合上持有的行锁/咨询锁。
            self.session.rollback()
            return None
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()
            return None
        return outcome

    def _authorize_delete_call_locked(self, claim, expected_checkpoint, operation_kind):
        """授权完整集合事务体；事务由调用方按返回值显式提交或回滚。

        不用 ``with session.begin()`` 包裹：worker 序列可能在两个仓库调用之间
        因过期属性刷新而 autobegin，begin() 会抛 InvalidRequestError 并被吞成
        授权拒绝。
        """
        from models import ImageAsset, ImageImportItem, PurgeBatch, PurgeBatchItem, PurgeObjectFence

        item = self.session.execute(select(PurgeBatchItem).where(
            PurgeBatchItem.batch_id == claim.batch_id,
            PurgeBatchItem.target_asset_id == claim.target_asset_id,
            PurgeBatchItem.claim_token == claim.claim_token,
            PurgeBatchItem.claim_generation == claim.claim_generation,
            PurgeBatchItem.status == 'in_progress',
            PurgeBatchItem.checkpoint == expected_checkpoint,
        ).with_for_update()).scalar_one_or_none()
        if item is None or not item.formal_bucket:
            return None
        now = self.session.execute(text('SELECT clock_timestamp()::timestamp')).scalar_one()
        if item.lease_expires_at is None or item.lease_expires_at <= now:
            return None
        batch = self.session.execute(select(PurgeBatch).where(
            PurgeBatch.id == item.batch_id, PurgeBatch.status == 'deleting'
        ).with_for_update()).scalar_one_or_none()
        asset = self.session.execute(select(ImageAsset).where(
            ImageAsset.id == item.target_asset_id, ImageAsset.status == 'archived',
            ImageAsset.oss_path == item.original_formal_key,
            ImageAsset.preview_oss_path == item.preview_formal_key,
        ).with_for_update()).scalar_one_or_none()
        if batch is None or asset is None or not self.manifest_validator(batch, item):
            return None
        if operation_kind == 'original':
            refs = self.session.execute(select(ImageImportItem.id).where(
                ImageImportItem.oss_path == item.original_formal_key,
                ImageImportItem.objects_purged_at.is_(None),
                # 排除目标自身已提升的完成项；asset_id 为 NULL（未提升）的
                # 未清除引用同样构成引用，必须阻止原图独占删除。
                ~and_(
                    ImageImportItem.status == 'completed',
                    ImageImportItem.asset_id == item.target_asset_id,
                ),
            ).with_for_update()).scalars().all()
            if refs:
                return None
        if operation_kind == 'preview':
            refs = self.session.execute(select(ImageAsset.id).where(
                ImageAsset.preview_oss_path == item.preview_formal_key,
                ImageAsset.id != item.target_asset_id,
            ).with_for_update()).scalars().all()
            import_refs = self.session.execute(select(ImageImportItem.id).where(
                ImageImportItem.preview_oss_path == item.preview_formal_key,
                ImageImportItem.objects_purged_at.is_(None),
                ~and_(ImageImportItem.status == 'completed', ImageImportItem.asset_id == item.target_asset_id),
            ).with_for_update()).scalars().all()
            if refs or import_refs or not item.preview_delete_authorized:
                return None
        keys = (item.original_formal_key, item.preview_formal_key)
        for key in sorted(keys):
            self.session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:v, 0))'), {'v': f'{item.formal_bucket}:{key}'})
        # 与绑定写入的互斥：同一咨询锁事务内复核正式键上不存在活的绑定租约；
        # 过期 held epoch 按协议回收（assert 内部落为 lease_expired 后放行）。
        from services.object_binding_fence import ObjectBindingFenceService
        from services.purge_object_fence import ObjectIdentity

        ObjectBindingFenceService(self.session).assert_purge_available((
            ObjectIdentity(item.formal_bucket, item.original_formal_key),
            ObjectIdentity(item.formal_bucket, item.preview_formal_key),
        ))
        fences = []
        for key, kind in ((item.original_formal_key, 'source_image'), (item.preview_formal_key, 'search_preview')):
            held = self.session.execute(select(PurgeObjectFence).where(
                PurgeObjectFence.formal_bucket == item.formal_bucket,
                PurgeObjectFence.formal_key == key,
                PurgeObjectFence.state == 'held',
            ).with_for_update()).scalar_one_or_none()
            if held is not None and (held.batch_id != item.batch_id or held.target_asset_id != item.target_asset_id):
                return None
            if held is None:
                held = PurgeObjectFence(formal_bucket=item.formal_bucket, formal_key=key, kind=kind, batch_id=item.batch_id, target_asset_id=item.target_asset_id, acquired_at=now, audit_retain_until=item.audit_retain_until or now + timedelta(days=365))
                self.session.add(held)
            fences.append(held)
        item.lease_expires_at = now + timedelta(seconds=60)
        self.session.flush()
        return tuple(fence.id for fence in fences)

    def checkpoint(self, claim, checkpoint, **kwargs):
        from models import PurgeBatch, PurgeBatchItem

        now = self.clock()
        try:
            item = self._locked_current(claim)
            if item is None:
                return False
            batch = self.session.execute(
                select(PurgeBatch).where(PurgeBatch.id == item.batch_id).with_for_update()
            ).scalar_one()
            if batch.status not in ('pending_deletion', 'deleting'):
                self.session.rollback()
                return False
            if checkpoint == 'original_delete_started' and batch.status == 'pending_deletion':
                batch.status = 'deleting'
                batch.deleting_at = now
            item.checkpoint = checkpoint
            item.checkpoint_at = now
            item.result_code = kwargs.get('result_code')
            self._event(item, f'purge.item.{checkpoint}', now, result_code=item.result_code)
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise

    def preview_is_shared(self, claim):
        from models import ImageAsset, ImageImportItem

        try:
            item = self._locked_current(claim)
            if item is None:
                return True
            asset_refs = self.session.execute(
                select(ImageAsset.id).where(
                    ImageAsset.preview_oss_path == item.preview_formal_key,
                    ImageAsset.id != item.target_asset_id,
                ).with_for_update()
            ).scalars().all()
            import_refs = self.session.execute(
                select(ImageImportItem.id).where(
                    ImageImportItem.preview_oss_path == item.preview_formal_key,
                    ImageImportItem.objects_purged_at.is_(None),
                    ~and_(
                        ImageImportItem.status == 'completed',
                        ImageImportItem.asset_id == item.target_asset_id,
                    ),
                ).with_for_update()
            ).scalars().all()
            # 结果必须在 rollback 之前完全求值：rollback 会 expire ORM 实例，
            # 之后再触碰 item 属性会 autobegin 新事务，让本方法带着未结束
            # 的 session 事务返回，使随后 authorize 的 session.begin() 抛
            # InvalidRequestError（重试从 preview_delete_started 恢复、
            # 且没有中间 checkpoint 提交时暴露）。
            shared = bool(asset_refs or import_refs) or not item.preview_delete_authorized
            self.session.rollback()
            return shared
        except Exception:
            self.session.rollback()
            raise

    def finalize(self, claim):
        from models import ImageAsset, ImageImportItem

        now = self.clock()
        try:
            item = self._locked_current(claim)
            if item is None:
                return False
            if item.checkpoint not in ('preview_deleted', 'preview_shared'):
                self.session.rollback()
                return False
            asset = self.session.execute(
                select(ImageAsset).where(ImageAsset.id == item.target_asset_id).with_for_update()
            ).scalar_one_or_none()
            if asset is None or asset.status != 'archived':
                self.session.rollback()
                return False
            self.session.execute(
                select(ImageImportItem)
                .where(ImageImportItem.asset_id == asset.id, ImageImportItem.status == 'completed')
                .with_for_update()
            ).scalars().all()
            for import_item in self.session.execute(
                select(ImageImportItem).where(
                    ImageImportItem.asset_id == asset.id,
                    ImageImportItem.status == 'completed',
                )
            ).scalars():
                import_item.objects_purged_at = now
            self.session.delete(asset)
            item.database_deleted_at = now
            item.status = 'completed'
            item.checkpoint = 'completed'
            item.completed_at = now
            item.claim_token = None
            item.lease_expires_at = None
            from models import PurgeObjectFence
            fences = self.session.execute(select(PurgeObjectFence).where(
                PurgeObjectFence.batch_id == item.batch_id,
                PurgeObjectFence.target_asset_id == item.target_asset_id,
                PurgeObjectFence.state == 'held',
            ).with_for_update()).scalars().all()
            for fence in fences:
                fence.state = 'released'
                fence.released_at = now
            self._event(item, 'purge.item.completed', now)
            self._reduce_batch(item.batch_id, now)
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise

    def fail(self, claim, error_code, *, retryable=True):
        from models import PurgeBatchItem

        now = self.clock()
        try:
            item = self._locked_current(claim)
            if item is None:
                return False
            item.status = 'failed'
            item.error_code = error_code
            item.result_code = 'retryable' if retryable else 'nonretryable'
            item.failed_at = now
            item.claim_token = None
            item.lease_expires_at = None
            self._event(item, 'purge.item.failed', now, error_code=error_code)
            self._reduce_batch(item.batch_id, now)
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise

    def retry_item(self, batch_id, target_asset_id):
        from models import PurgeBatchItem

        now = self.clock()
        try:
            item = self.session.execute(
                select(PurgeBatchItem).where(
                    PurgeBatchItem.batch_id == batch_id,
                    PurgeBatchItem.target_asset_id == target_asset_id,
                    PurgeBatchItem.status == 'failed',
                ).with_for_update()
            ).scalar_one_or_none()
            if item is None:
                self.session.rollback()
                return False
            item.status = 'pending'
            item.error_code = None
            self._event(item, 'purge.item.retried', now)
            self._reduce_batch(batch_id, now)
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise

    def _reduce_batch(self, batch_id, now):
        from models import PurgeBatch, PurgeBatchItem

        batch = self.session.execute(
            select(PurgeBatch).where(PurgeBatch.id == batch_id).with_for_update()
        ).scalar_one()
        statuses = self.session.execute(
            select(PurgeBatchItem.status).where(PurgeBatchItem.batch_id == batch_id)
        ).scalars().all()
        if statuses and all(status == 'completed' for status in statuses):
            batch.status = 'completed'
            batch.completed_at = now
        elif any(status == 'failed' for status in statuses):
            failed_codes = self.session.execute(
                select(PurgeBatchItem.result_code).where(
                    PurgeBatchItem.batch_id == batch_id,
                    PurgeBatchItem.status == 'failed',
                )
            ).scalars().all()
            if any(code == 'retryable' for code in failed_codes):
                batch.status = 'deleting'
            else:
                batch.status = 'partial_failure'
                batch.partial_failure_at = now

    def _locked_current(self, claim):
        from models import PurgeBatchItem

        now = self.session.execute(text('SELECT clock_timestamp()::timestamp')).scalar_one()
        item = self.session.execute(
            select(PurgeBatchItem).where(
                PurgeBatchItem.batch_id == claim.batch_id,
                PurgeBatchItem.target_asset_id == claim.target_asset_id,
                PurgeBatchItem.claim_token == claim.claim_token,
                PurgeBatchItem.claim_generation == claim.claim_generation,
                PurgeBatchItem.status == 'in_progress',
                PurgeBatchItem.lease_expires_at > now,
            ).with_for_update()
        ).scalar_one_or_none()
        if item is None:
            self.session.rollback()
        return item

    def _event(self, item, event_type, now, *, result_code=None, error_code=None):
        from models import PurgeItemEvent

        self.session.add(PurgeItemEvent(
            batch_id=item.batch_id,
            target_asset_id=item.target_asset_id,
            event_type=event_type,
            result_code=result_code,
            error_code=error_code,
            created_at=now,
            audit_retain_until=now + timedelta(days=365),
        ))


class FormalPurgeWorker:
    def __init__(self, *, repository, capability, deleter):
        self._repository = repository
        self._capability = capability
        self._deleter = deleter

    def process_one_item(self) -> bool:
        """Hard gate precedes even a queue claim in T13."""
        if not self._capability.evaluate():
            return False
        claim = self._repository.claim_next_item()
        if claim is None:
            return False
        try:
            checkpoint = self._value(claim, 'checkpoint', 'checkpoint')
            if checkpoint in ('pending', 'original_delete_started'):
                if checkpoint == 'pending':
                    if not self._repository.checkpoint(claim, 'original_delete_started'):
                        raise RuntimeError('stale original intent')
                if not self._repository.authorize_delete_call(claim, 'original_delete_started', {'verified': True}, 'original'):
                    raise RuntimeError('original delete unauthorized')
                self._deleter.delete_if_present(self._value(claim, 'original', 'original_key'))
                if not self._repository.checkpoint(claim, 'original_deleted'):
                    raise RuntimeError('stale original checkpoint')
                checkpoint = 'original_deleted'
            if checkpoint in ('original_deleted', 'preview_delete_started'):
                if self._repository.preview_is_shared(claim):
                    self._repository.checkpoint(claim, 'preview_shared')
                else:
                    if checkpoint == 'original_deleted':
                        if not self._repository.checkpoint(claim, 'preview_delete_started'):
                            raise RuntimeError('stale preview intent')
                    if not self._repository.authorize_delete_call(claim, 'preview_delete_started', {'verified': True}, 'preview'):
                        raise RuntimeError('preview delete unauthorized')
                    self._deleter.delete_if_present(self._value(claim, 'preview', 'preview_key'))
                    if not self._repository.checkpoint(claim, 'preview_deleted'):
                        raise RuntimeError('stale preview checkpoint')
            if not self._repository.finalize(claim):
                raise RuntimeError('formal purge finalization rejected')
            self._repository.checkpoint(claim, 'completed')
        except Exception:
            failure = getattr(self._repository, 'fail', None)
            if failure is None:
                raise
            failure(claim, 'PURGE_OBJECT_DELETE_FAILED')
        return True

    @staticmethod
    def _value(claim, mapping_key, attribute):
        if isinstance(claim, dict):
            return claim.get(mapping_key, 'pending' if mapping_key == 'checkpoint' else None)
        return getattr(claim, attribute)
