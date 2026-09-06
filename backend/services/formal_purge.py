"""Checkpoint orchestration seam for #27; T13 production capability is off."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, text


class CanonicalFormalPurgeAuthorizationVerifier:
    """Fail-closed typed verifier for canonical manifest/copy/item evidence."""

    def __init__(
        self,
        *,
        manifest_loader,
        verify_copies,
        restore_point_loader,
        clock=datetime.now,
    ):
        self._manifest_loader = manifest_loader
        self._verify_copies = verify_copies
        self._restore_point_loader = restore_point_loader
        self._clock = clock

    def verify_for_operation(self, batch, item, stage):
        try:
            from services.purge_formal_authorization import (
                build_formal_purge_authorization_bundle,
            )
            from services.purge_object_backup import VerifiedPurgeObjectBackup

            if stage not in {'claim', 'original', 'preview', 'finalize'}:
                return False
            now = _as_utc_datetime(self._clock())
            if batch.retain_until is None or item.authorization_retain_until is None:
                return False
            if (
                _as_utc_datetime(batch.retain_until) <= now
                or _as_utc_datetime(item.authorization_retain_until) <= now
            ):
                return False
            evidence = self._manifest_loader(batch)
            if (
                not isinstance(evidence, VerifiedPurgeObjectBackup)
                or evidence.status != 'complete'
                or evidence.manifest_sha256 != batch.object_manifest_sha256
            ):
                return False
            copy_result = self._verify_copies(evidence.manifest)
            copies_verified = bool(
                copy_result is True
                or (
                    getattr(copy_result, 'status', None) == 'verified'
                    and getattr(copy_result, 'manifest_sha256', None)
                    == evidence.manifest_sha256
                    and getattr(copy_result, 'object_count', None)
                    == len(evidence.manifest.objects)
                )
            )
            if not copies_verified:
                return False
            if not self._live_restore_point_matches(batch, evidence.manifest, now):
                return False
            bundle = build_formal_purge_authorization_bundle(
                evidence.manifest,
                manifest_sha256=evidence.manifest_sha256,
                now=now,
            )
            if (
                bundle.purge_batch_id != uuid.UUID(str(batch.id))
                or batch.database_backup_id != bundle.database_backup_id
                or batch.database_manifest_sha256 != bundle.database_manifest_sha256
                or _as_utc_datetime(batch.retain_until) != bundle.retain_until
            ):
                return False
            authorization = next(
                (
                    candidate
                    for candidate in bundle.items
                    if candidate.target_asset_id == uuid.UUID(str(item.target_asset_id))
                ),
                None,
            )
            if authorization is None:
                return False
            exact = (
                item.formal_bucket == authorization.formal_bucket
                and item.original_formal_key == authorization.original_formal_key
                and item.original_backup_object_id == authorization.original_backup_object_id
                and item.original_backup_sha256 == authorization.original_backup_sha256
                and item.preview_formal_key == authorization.preview_formal_key
                and item.preview_backup_object_id == authorization.preview_backup_object_id
                and item.preview_backup_sha256 == authorization.preview_backup_sha256
                and item.preview_delete_authorized == authorization.preview_delete_authorized
                and _as_utc_datetime(item.authorization_retain_until)
                == authorization.authorization_retain_until
            )
            if not exact or (stage == 'preview' and not authorization.preview_delete_authorized):
                return False
            return True
        except Exception:
            return False

    def _live_restore_point_matches(self, batch, object_manifest, now):
        restore_point = self._restore_point_loader(batch)
        if restore_point is None:
            return False
        kind = getattr(restore_point, 'kind', None)
        backup_id = getattr(restore_point, 'backup_id', None)
        artifact_sha256 = getattr(restore_point, 'artifact_sha256', None)
        retain_until = getattr(restore_point, 'retain_until', None)
        if (
            kind != 'purge_restore_point'
            or not backup_id
            or not artifact_sha256
            or retain_until is None
        ):
            return False
        live_retain_until = _as_utc_datetime(retain_until)
        if live_retain_until <= now:
            return False
        object_restore_point = getattr(object_manifest, 'database_restore_point', None)
        if not isinstance(object_restore_point, dict):
            return False
        object_retain_until = _as_utc_datetime(
            datetime.fromisoformat(
                str(object_restore_point['retain_until']).replace('Z', '+00:00')
            )
        )
        return (
            backup_id == batch.database_backup_id
            and backup_id == object_restore_point['backup_id']
            and artifact_sha256 == object_restore_point['artifact_sha256']
            and live_retain_until == _as_utc_datetime(batch.retain_until)
            and live_retain_until == object_retain_until
        )


def _as_utc_datetime(value):
    """Database timestamps are naive UTC; evidence timestamps are aware UTC."""
    if not isinstance(value, datetime):
        raise ValueError('invalid timestamp')
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


@dataclass(frozen=True)
class DeleteCallAuthorization:
    permit_id: uuid.UUID
    grant_id: str
    batch_id: uuid.UUID
    target_asset_id: uuid.UUID
    claim_token: uuid.UUID
    claim_generation: int
    operation_kind: str
    formal_bucket: str
    formal_key: str
    fence_ids: tuple[uuid.UUID, uuid.UUID]
    observation: object
    authorized_at: datetime
    expires_at: datetime


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

    def begin_delete_intent(
        self,
        claim,
        *,
        operation_kind,
        observation,
        grant,
    ):
        """Atomically consume grant, fence identities, persist intent and permit."""
        from services.purge_object_storage import FormalObjectObservation
        from services.purge_formal_deletion_capability import FormalDeletionGrant

        if (
            claim is None
            or not isinstance(observation, FormalObjectObservation)
            or not isinstance(grant, FormalDeletionGrant)
            or operation_kind not in {'original', 'preview'}
        ):
            return None
        try:
            outcome = self._begin_delete_intent_locked(
                claim, operation_kind, observation, grant,
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

    def authorize_delete_call(
        self,
        claim,
        *,
        expected_checkpoint,
        operation_kind,
        observation,
        grant=None,
    ):
        """Compatibility name; safe only with a typed persisted grant."""
        expected = {
            'original': 'original_delete_started',
            'preview': 'preview_delete_started',
        }.get(operation_kind)
        if expected_checkpoint != expected:
            return None
        return self.begin_delete_intent(
            claim,
            operation_kind=operation_kind,
            observation=observation,
            grant=grant,
        )

    def _begin_delete_intent_locked(
        self, claim, operation_kind, observation, grant,
    ):
        """授权完整集合事务体；事务由调用方按返回值显式提交或回滚。

        不用 ``with session.begin()`` 包裹：worker 序列可能在两个仓库调用之间
        因过期属性刷新而 autobegin，begin() 会抛 InvalidRequestError 并被吞成
        授权拒绝。
        """
        from models import (
            FormalDeleteCallPermit,
            ImageAsset,
            ImageImportItem,
            PurgeBatch,
            PurgeBatchItem,
            PurgeObjectFence,
        )

        allowed_checkpoints = (
            ('pending', 'original_delete_started')
            if operation_kind == 'original'
            else ('original_deleted', 'preview_delete_started')
        )

        item = self.session.execute(select(PurgeBatchItem).where(
            PurgeBatchItem.batch_id == claim.batch_id,
            PurgeBatchItem.target_asset_id == claim.target_asset_id,
            PurgeBatchItem.claim_token == claim.claim_token,
            PurgeBatchItem.claim_generation == claim.claim_generation,
            PurgeBatchItem.status == 'in_progress',
            PurgeBatchItem.checkpoint.in_(allowed_checkpoints),
        ).with_for_update()).scalar_one_or_none()
        if item is None or not item.formal_bucket:
            return None
        now = self.session.execute(text('SELECT clock_timestamp()::timestamp')).scalar_one()
        if item.lease_expires_at is None or item.lease_expires_at <= now:
            return None
        allowed_batch_statuses = (
            ('pending_deletion', 'deleting')
            if operation_kind == 'original'
            else ('deleting',)
        )
        batch = self.session.execute(select(PurgeBatch).where(
            PurgeBatch.id == item.batch_id,
            PurgeBatch.status.in_(allowed_batch_statuses),
        ).with_for_update()).scalar_one_or_none()
        asset = self.session.execute(select(ImageAsset).where(
            ImageAsset.id == item.target_asset_id, ImageAsset.status == 'archived',
            ImageAsset.oss_path == item.original_formal_key,
            ImageAsset.preview_oss_path == item.preview_formal_key,
        ).with_for_update()).scalar_one_or_none()
        if batch is None or asset is None or not self.manifest_validator(batch, item):
            return None
        expected_key = (
            item.original_formal_key
            if operation_kind == 'original'
            else item.preview_formal_key
        )
        expected_sha256 = (
            item.original_backup_sha256
            if operation_kind == 'original'
            else item.preview_backup_sha256
        )
        if (
            observation.formal_bucket != item.formal_bucket
            or observation.formal_key != expected_key
            or observation.sha256 != expected_sha256
        ):
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
        self.session.flush()
        fence_ids = tuple(sorted(fence.id for fence in fences))
        if len(fence_ids) != 2 or len(set(fence_ids)) != 2:
            return None
        consumption = self._consume_grant_locked(grant, batch, item, now)
        if consumption is None:
            return None
        permit = self.session.execute(
            select(FormalDeleteCallPermit).where(
                FormalDeleteCallPermit.batch_id == item.batch_id,
                FormalDeleteCallPermit.target_asset_id == item.target_asset_id,
                FormalDeleteCallPermit.operation_kind == operation_kind,
            ).with_for_update()
        ).scalar_one_or_none()
        permit_expiry = min(
            consumption.expires_at,
            batch.retain_until,
            item.authorization_retain_until,
            now + timedelta(seconds=60),
        )
        if permit_expiry <= now:
            return None
        intent_checkpoint = (
            'original_delete_started'
            if operation_kind == 'original'
            else 'preview_delete_started'
        )
        if permit is None:
            if consumption.used_object_deletes >= consumption.max_object_deletes:
                return None
            permit = FormalDeleteCallPermit(
                id=uuid.uuid4(),
                grant_id=consumption.grant_id,
                batch_id=item.batch_id,
                target_asset_id=item.target_asset_id,
                operation_kind=operation_kind,
                claim_generation=item.claim_generation,
                formal_bucket=item.formal_bucket,
                formal_key=expected_key,
                object_size=observation.size,
                object_sha256=observation.sha256,
                object_etag=observation.etag,
                original_fence_id=fences[0].id,
                preview_fence_id=fences[1].id,
                state='issued',
                issued_at=now,
                expires_at=permit_expiry,
                audit_retain_until=now + timedelta(days=365),
            )
            self.session.add(permit)
            consumption.used_object_deletes += 1
        elif not self._permit_matches(
            permit, item, observation, fence_ids, consumption.grant_id,
        ):
            return None
        if item.checkpoint != intent_checkpoint:
            item.checkpoint = intent_checkpoint
            item.checkpoint_at = now
            if operation_kind == 'original':
                item.original_delete_started_at = now
                if batch.status == 'pending_deletion':
                    batch.status = 'deleting'
                    batch.deleting_at = now
            else:
                item.preview_delete_started_at = now
            self._event(item, f'purge.item.{intent_checkpoint}', now)
        item.lease_expires_at = min(permit_expiry, now + timedelta(seconds=60))
        self.session.flush()
        return DeleteCallAuthorization(
            permit_id=permit.id,
            grant_id=consumption.grant_id,
            batch_id=item.batch_id,
            target_asset_id=item.target_asset_id,
            claim_token=item.claim_token,
            claim_generation=item.claim_generation,
            operation_kind=operation_kind,
            formal_bucket=item.formal_bucket,
            formal_key=expected_key,
            fence_ids=fence_ids,
            observation=observation,
            authorized_at=_as_utc_datetime(permit.issued_at),
            expires_at=_as_utc_datetime(permit.expires_at),
        )

    def _consume_grant_locked(self, grant, batch, item, now):
        from models import FormalDeletionGrantConsumption

        context = grant.context
        if (
            context.batch_id != batch.id
            or item.target_asset_id not in context.asset_ids
            or context.database_manifest_sha256 != batch.database_manifest_sha256
            or context.object_manifest_sha256 != batch.object_manifest_sha256
            or context.formal_bucket != item.formal_bucket
        ):
            return None
        # Existing purge tables store naive wall-clock timestamps. Convert the
        # aware grant into the process/DB deployment timezone before comparing
        # with ``clock_timestamp()::timestamp``.
        issued_at = grant.issued_at.astimezone().replace(tzinfo=None)
        expires_at = grant.expires_at.astimezone().replace(tzinfo=None)
        if issued_at > now + timedelta(seconds=60) or expires_at <= now:
            return None
        asset_scope_sha256 = hashlib.sha256(json.dumps(
            [str(value) for value in context.asset_ids],
            separators=(',', ':'),
        ).encode('utf-8')).hexdigest()
        consumption = self.session.execute(
            select(FormalDeletionGrantConsumption).where(
                FormalDeletionGrantConsumption.grant_id == grant.grant_id,
            ).with_for_update()
        ).scalar_one_or_none()
        batch_consumption = self.session.execute(
            select(FormalDeletionGrantConsumption).where(
                FormalDeletionGrantConsumption.batch_id == batch.id,
            ).with_for_update()
        ).scalar_one_or_none()
        if consumption is None:
            if batch_consumption is not None:
                return None
            consumption = FormalDeletionGrantConsumption(
                grant_id=grant.grant_id,
                batch_id=batch.id,
                environment_id=context.environment_id,
                deployment_sha256=context.deployment_sha256,
                database_manifest_sha256=context.database_manifest_sha256,
                object_manifest_sha256=context.object_manifest_sha256,
                formal_bucket=context.formal_bucket,
                asset_scope_sha256=asset_scope_sha256,
                max_assets=len(context.asset_ids),
                max_object_deletes=grant.max_object_deletes,
                used_object_deletes=0,
                issued_at=issued_at,
                expires_at=expires_at,
                consumed_at=now,
                state='active',
                trust_attestation_sha256=grant.trust_attestation_sha256,
                audit_retain_until=now + timedelta(days=365),
            )
            self.session.add(consumption)
            self.session.flush()
            return consumption
        if batch_consumption is not None and batch_consumption.grant_id != grant.grant_id:
            return None
        expected = (
            consumption.batch_id == batch.id
            and consumption.environment_id == context.environment_id
            and consumption.deployment_sha256 == context.deployment_sha256
            and consumption.database_manifest_sha256 == context.database_manifest_sha256
            and consumption.object_manifest_sha256 == context.object_manifest_sha256
            and consumption.formal_bucket == context.formal_bucket
            and consumption.asset_scope_sha256 == asset_scope_sha256
            and consumption.max_assets == len(context.asset_ids)
            and consumption.max_object_deletes == grant.max_object_deletes
            and consumption.expires_at == expires_at
            and consumption.state == 'active'
            and consumption.trust_attestation_sha256 == grant.trust_attestation_sha256
        )
        return consumption if expected else None

    @staticmethod
    def _permit_matches(permit, item, observation, fence_ids, grant_id):
        return bool(
            permit.grant_id == grant_id
            and permit.batch_id == item.batch_id
            and permit.target_asset_id == item.target_asset_id
            and permit.claim_generation == item.claim_generation
            and permit.formal_bucket == item.formal_bucket
            and permit.formal_key == observation.formal_key
            and permit.object_size == observation.size
            and permit.object_sha256 == observation.sha256
            and permit.object_etag == observation.etag
            and {
                permit.original_fence_id, permit.preview_fence_id,
            } == set(fence_ids)
            and permit.state in ('issued', 'executing')
        )

    def start_delete_call(self, authorization):
        """Atomically mark one persisted permit executing after revalidation."""
        if not isinstance(authorization, DeleteCallAuthorization):
            return None
        try:
            locked = self._lock_valid_permit(authorization)
            if locked is None:
                self.session.rollback()
                return None
            permit, _grant, item, now = locked
            if permit.state == 'issued':
                permit.state = 'executing'
                permit.executing_at = now
                self._event(
                    item, 'purge.item.delete_call_executing', now,
                    result_code=authorization.operation_kind,
                )
            self.session.commit()
            return authorization
        except Exception:
            self.session.rollback()
            return None

    def resume_delete_intent(self, claim, *, operation_kind, grant):
        """Rebind an existing permit to a reclaimed lease without re-consuming."""
        from models import FormalDeleteCallPermit, PurgeBatch, PurgeBatchItem
        from services.purge_formal_deletion_capability import FormalDeletionGrant
        from services.purge_object_storage import FormalObjectObservation

        if (
            claim is None
            or operation_kind not in {'original', 'preview'}
            or not isinstance(grant, FormalDeletionGrant)
        ):
            return None
        now = self.session.execute(
            text('SELECT clock_timestamp()::timestamp')
        ).scalar_one()
        checkpoint = (
            'original_delete_started'
            if operation_kind == 'original'
            else 'preview_delete_started'
        )
        try:
            item = self.session.execute(select(PurgeBatchItem).where(
                PurgeBatchItem.batch_id == claim.batch_id,
                PurgeBatchItem.target_asset_id == claim.target_asset_id,
                PurgeBatchItem.claim_token == claim.claim_token,
                PurgeBatchItem.claim_generation == claim.claim_generation,
                PurgeBatchItem.status == 'in_progress',
                PurgeBatchItem.checkpoint == checkpoint,
                PurgeBatchItem.lease_expires_at > now,
            ).with_for_update()).scalar_one_or_none()
            batch = self.session.execute(select(PurgeBatch).where(
                PurgeBatch.id == claim.batch_id,
                PurgeBatch.status == 'deleting',
            ).with_for_update()).scalar_one_or_none()
            if item is None or batch is None or not self.manifest_validator(batch, item):
                self.session.rollback()
                return None
            consumption = self._consume_grant_locked(grant, batch, item, now)
            if consumption is None:
                self.session.rollback()
                return None
            permit = self.session.execute(select(FormalDeleteCallPermit).where(
                FormalDeleteCallPermit.grant_id == consumption.grant_id,
                FormalDeleteCallPermit.batch_id == item.batch_id,
                FormalDeleteCallPermit.target_asset_id == item.target_asset_id,
                FormalDeleteCallPermit.operation_kind == operation_kind,
            ).with_for_update()).scalar_one_or_none()
            if permit is None or permit.state not in ('issued', 'executing'):
                self.session.rollback()
                return None
            permit.claim_generation = item.claim_generation
            observation = FormalObjectObservation(
                formal_bucket=permit.formal_bucket,
                formal_key=permit.formal_key,
                size=permit.object_size,
                sha256=permit.object_sha256,
                etag=permit.object_etag,
                observed_at=_as_utc_datetime(permit.issued_at),
            )
            authorization = DeleteCallAuthorization(
                permit_id=permit.id,
                grant_id=permit.grant_id,
                batch_id=item.batch_id,
                target_asset_id=item.target_asset_id,
                claim_token=item.claim_token,
                claim_generation=item.claim_generation,
                operation_kind=operation_kind,
                formal_bucket=permit.formal_bucket,
                formal_key=permit.formal_key,
                fence_ids=tuple(sorted((
                    permit.original_fence_id, permit.preview_fence_id,
                ))),
                observation=observation,
                authorized_at=_as_utc_datetime(permit.issued_at),
                expires_at=_as_utc_datetime(permit.expires_at),
            )
            if self._lock_valid_permit(authorization) is None:
                self.session.rollback()
                return None
            self.session.commit()
            return authorization
        except Exception:
            self.session.rollback()
            return None

    def confirm_absent_after_intent(self, authorization):
        """Complete a replayed intent only after its permit/fences revalidate."""
        if not isinstance(authorization, DeleteCallAuthorization):
            return False
        try:
            locked = self._lock_valid_permit(authorization)
            if locked is None:
                self.session.rollback()
                return False
            permit, _grant, item, now = locked
            self._complete_permit_and_checkpoint(
                permit, item, now, result_code='already_absent_after_intent',
            )
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            return False

    def complete_delete_call(self, authorization, deletion_observation):
        from services.purge_object_storage import DeletionObservation

        if (
            not isinstance(authorization, DeleteCallAuthorization)
            or not isinstance(deletion_observation, DeletionObservation)
            or deletion_observation.before != authorization.observation
        ):
            return False
        try:
            locked = self._lock_valid_permit(authorization)
            if locked is None:
                self.session.rollback()
                return False
            permit, _grant, item, now = locked
            if permit.state != 'executing':
                self.session.rollback()
                return False
            self._complete_permit_and_checkpoint(
                permit, item, now, result_code=deletion_observation.result,
            )
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            return False

    def _lock_valid_permit(self, authorization):
        from models import (
            FormalDeleteCallPermit,
            FormalDeletionGrantConsumption,
            PurgeBatch,
            PurgeBatchItem,
            PurgeObjectFence,
        )

        now = self.session.execute(
            text('SELECT clock_timestamp()::timestamp')
        ).scalar_one()
        permit = self.session.execute(select(FormalDeleteCallPermit).where(
            FormalDeleteCallPermit.id == authorization.permit_id,
        ).with_for_update()).scalar_one_or_none()
        if permit is None:
            return None
        grant = self.session.execute(select(FormalDeletionGrantConsumption).where(
            FormalDeletionGrantConsumption.grant_id == permit.grant_id,
        ).with_for_update()).scalar_one_or_none()
        item = self.session.execute(select(PurgeBatchItem).where(
            PurgeBatchItem.batch_id == permit.batch_id,
            PurgeBatchItem.target_asset_id == permit.target_asset_id,
        ).with_for_update()).scalar_one_or_none()
        batch = self.session.execute(select(PurgeBatch).where(
            PurgeBatch.id == permit.batch_id,
            PurgeBatch.status == 'deleting',
        ).with_for_update()).scalar_one_or_none()
        expected_checkpoint = (
            'original_delete_started'
            if permit.operation_kind == 'original'
            else 'preview_delete_started'
        )
        if (
            grant is None or item is None or batch is None
            or grant.state != 'active'
            or grant.expires_at <= now
            or batch.retain_until is None or batch.retain_until <= now
            or item.authorization_retain_until is None
            or item.authorization_retain_until <= now
            or item.lease_expires_at is None or item.lease_expires_at <= now
            or permit.expires_at <= now
            or _as_utc_datetime(authorization.expires_at) <= _as_utc_datetime(now)
            or _as_utc_datetime(authorization.expires_at)
            != _as_utc_datetime(permit.expires_at)
            or permit.state not in ('issued', 'executing')
            or permit.grant_id != authorization.grant_id
            or permit.batch_id != authorization.batch_id
            or permit.target_asset_id != authorization.target_asset_id
            or permit.claim_generation != authorization.claim_generation
            or item.claim_generation != authorization.claim_generation
            or item.claim_token != authorization.claim_token
            or item.status != 'in_progress'
            or item.checkpoint != expected_checkpoint
            or permit.operation_kind != authorization.operation_kind
            or permit.formal_bucket != authorization.formal_bucket
            or permit.formal_key != authorization.formal_key
            or permit.object_size != authorization.observation.size
            or permit.object_sha256 != authorization.observation.sha256
            or permit.object_etag != authorization.observation.etag
        ):
            return None
        expected_fence_ids = tuple(sorted((
            permit.original_fence_id, permit.preview_fence_id,
        )))
        fences = self.session.execute(select(PurgeObjectFence).where(
            PurgeObjectFence.id.in_((
                permit.original_fence_id, permit.preview_fence_id,
            )),
            PurgeObjectFence.batch_id == permit.batch_id,
            PurgeObjectFence.target_asset_id == permit.target_asset_id,
            PurgeObjectFence.state == 'held',
        ).with_for_update()).scalars().all()
        if (
            len(fences) != 2
            or authorization.fence_ids != expected_fence_ids
        ):
            return None
        if not self.manifest_validator(batch, item):
            return None
        return permit, grant, item, now

    def _complete_permit_and_checkpoint(self, permit, item, now, *, result_code):
        checkpoint = (
            'original_deleted'
            if permit.operation_kind == 'original'
            else 'preview_deleted'
        )
        permit.state = 'completed'
        permit.executing_at = permit.executing_at or now
        permit.completed_at = now
        permit.result_code = result_code
        item.checkpoint = checkpoint
        item.checkpoint_at = now
        item.result_code = result_code
        if permit.operation_kind == 'original':
            item.original_deleted_at = now
        else:
            item.preview_deleted_at = now
            item.preview_disposition = 'deleted'
        self._event(item, f'purge.item.{checkpoint}', now, result_code=result_code)

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
            timestamp_field = {
                'original_delete_started': 'original_delete_started_at',
                'original_deleted': 'original_deleted_at',
                'preview_delete_started': 'preview_delete_started_at',
                'preview_deleted': 'preview_deleted_at',
            }.get(checkpoint)
            if timestamp_field is not None:
                setattr(item, timestamp_field, now)
            if checkpoint == 'preview_shared':
                item.preview_disposition = 'shared'
            elif checkpoint == 'preview_deleted':
                item.preview_disposition = 'deleted'
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
        from models import PurgeObjectFence

        now = self.clock()
        try:
            item = self._locked_current(claim)
            if item is None:
                return False
            item.status = 'failed'
            item.failed_at = now
            item.claim_token = None
            item.lease_expires_at = None
            if retryable:
                item.error_code = error_code
                item.result_code = 'retryable'
            elif item.checkpoint in ('pending', 'fenced'):
                item.error_code = error_code
                item.result_code = 'nonretryable'
                fences = self.session.execute(select(PurgeObjectFence).where(
                    PurgeObjectFence.batch_id == item.batch_id,
                    PurgeObjectFence.target_asset_id == item.target_asset_id,
                    PurgeObjectFence.state == 'held',
                ).with_for_update()).scalars().all()
                for fence in fences:
                    fence.state = 'released'
                    fence.released_at = now
            else:
                item.error_code = 'PURGE_REPROTECTION_REQUIRED'
                item.result_code = 'nonretryable'
            self._event(item, 'purge.item.failed', now, error_code=item.error_code)
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

    def reconcile_expired_authorizations(self, *, limit=100):
        """Stop expired work and make its required recovery action explicit."""
        from models import PurgeBatch, PurgeBatchItem, PurgeObjectFence

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('expired authorization limit must be 1..100')
        now = self.session.execute(
            text('SELECT clock_timestamp()::timestamp')
        ).scalar_one()
        try:
            items = self.session.execute(
                select(PurgeBatchItem)
                .join(PurgeBatch, PurgeBatch.id == PurgeBatchItem.batch_id)
                .where(
                    PurgeBatch.status.in_(('pending_deletion', 'deleting')),
                    PurgeBatchItem.status.in_(('pending', 'in_progress', 'failed')),
                    or_(
                        PurgeBatch.retain_until <= now,
                        PurgeBatchItem.authorization_retain_until <= now,
                    ),
                )
                .order_by(PurgeBatch.created_at, PurgeBatchItem.ordinal)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).scalars().all()
            for item in items:
                self.session.execute(
                    select(PurgeBatch)
                    .where(PurgeBatch.id == item.batch_id)
                    .with_for_update()
                ).scalar_one()
                item.status = 'failed'
                item.failed_at = now
                item.claim_token = None
                item.lease_expires_at = None
                if item.checkpoint in ('pending', 'fenced'):
                    fences = self.session.execute(
                        select(PurgeObjectFence).where(
                            PurgeObjectFence.batch_id == item.batch_id,
                            PurgeObjectFence.target_asset_id == item.target_asset_id,
                            PurgeObjectFence.state == 'held',
                        ).with_for_update()
                    ).scalars().all()
                    for fence in fences:
                        fence.state = 'released'
                        fence.released_at = now
                    item.result_code = 'reprotected'
                    item.error_code = 'PURGE_BACKUP_RETENTION_EXPIRED'
                    event_type = 'purge.item.retention_expired_safe'
                else:
                    item.result_code = 'nonretryable'
                    item.error_code = 'PURGE_REPROTECTION_REQUIRED'
                    event_type = 'purge.item.reprotection_required'
                self._event(
                    item,
                    event_type,
                    now,
                    result_code=item.result_code,
                    error_code=item.error_code,
                )
                self._reduce_batch(item.batch_id, now)
            self.session.commit()
            return len(items)
        except Exception:
            self.session.rollback()
            raise

    def confirm_reprotected(
        self,
        batch_id,
        target_asset_id,
        *,
        original_observation,
        preview_observation=None,
    ):
        """Release deletion fences only after exact restored bytes are observed."""
        from models import (
            FormalDeleteCallPermit,
            ImageAsset,
            PurgeBatchItem,
            PurgeObjectFence,
        )
        from services.purge_object_storage import FormalObjectObservation

        if not isinstance(original_observation, FormalObjectObservation):
            return False
        if preview_observation is not None and not isinstance(
            preview_observation, FormalObjectObservation,
        ):
            return False
        now = self.session.execute(
            text('SELECT clock_timestamp()::timestamp')
        ).scalar_one()
        try:
            item = self.session.execute(
                select(PurgeBatchItem).where(
                    PurgeBatchItem.batch_id == batch_id,
                    PurgeBatchItem.target_asset_id == target_asset_id,
                    PurgeBatchItem.status == 'failed',
                    PurgeBatchItem.error_code == 'PURGE_REPROTECTION_REQUIRED',
                ).with_for_update()
            ).scalar_one_or_none()
            if item is None:
                self.session.rollback()
                return False
            permits = self.session.execute(
                select(FormalDeleteCallPermit).where(
                    FormalDeleteCallPermit.batch_id == item.batch_id,
                    FormalDeleteCallPermit.target_asset_id == item.target_asset_id,
                ).with_for_update()
            ).scalars().all()
            if any(permit.state in ('issued', 'executing') for permit in permits):
                self.session.rollback()
                return False
            asset = self.session.execute(
                select(ImageAsset).where(
                    ImageAsset.id == item.target_asset_id,
                    ImageAsset.status == 'archived',
                    ImageAsset.oss_path == item.original_formal_key,
                    ImageAsset.preview_oss_path == item.preview_formal_key,
                ).with_for_update()
            ).scalar_one_or_none()
            original_matches = (
                asset is not None
                and original_observation.formal_bucket == item.formal_bucket
                and original_observation.formal_key == item.original_formal_key
                and original_observation.sha256 == item.original_backup_sha256
            )
            preview_matches = True
            if item.preview_backup_sha256:
                preview_matches = bool(
                    preview_observation is not None
                    and preview_observation.formal_bucket == item.formal_bucket
                    and preview_observation.formal_key == item.preview_formal_key
                    and preview_observation.sha256 == item.preview_backup_sha256
                )
            if not original_matches or not preview_matches:
                self.session.rollback()
                return False
            fences = self.session.execute(
                select(PurgeObjectFence).where(
                    PurgeObjectFence.batch_id == item.batch_id,
                    PurgeObjectFence.target_asset_id == item.target_asset_id,
                    PurgeObjectFence.state == 'held',
                ).with_for_update()
            ).scalars().all()
            for fence in fences:
                fence.state = 'released'
                fence.released_at = now
            item.result_code = 'reprotected'
            item.error_code = 'PURGE_BACKUP_RETENTION_EXPIRED'
            self._event(
                item,
                'purge.item.reprotected',
                now,
                result_code=item.result_code,
                error_code=item.error_code,
            )
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            raise

    def cancel_issued_permits_for_reprotection(self, batch_id, target_asset_id):
        """Neutralize issued permits; executing calls require outcome recovery."""
        from models import (
            FormalDeleteCallPermit,
            FormalDeletionGrantConsumption,
            PurgeBatchItem,
        )

        now = self.session.execute(
            text('SELECT clock_timestamp()::timestamp')
        ).scalar_one()
        try:
            item = self.session.execute(select(PurgeBatchItem).where(
                PurgeBatchItem.batch_id == batch_id,
                PurgeBatchItem.target_asset_id == target_asset_id,
                PurgeBatchItem.status == 'failed',
                PurgeBatchItem.error_code == 'PURGE_REPROTECTION_REQUIRED',
            ).with_for_update()).scalar_one_or_none()
            if item is None:
                self.session.rollback()
                return False
            permits = self.session.execute(select(FormalDeleteCallPermit).where(
                FormalDeleteCallPermit.batch_id == batch_id,
                FormalDeleteCallPermit.target_asset_id == target_asset_id,
            ).with_for_update()).scalars().all()
            if (
                not permits
                or any(permit.state == 'executing' for permit in permits)
                or not any(permit.state == 'issued' for permit in permits)
            ):
                self.session.rollback()
                return False
            grant_ids = set()
            for permit in permits:
                grant_ids.add(permit.grant_id)
                if permit.state == 'issued':
                    permit.state = 'cancelled'
                    permit.cancelled_at = now
                    permit.result_code = 'reprotection_cancelled_before_delete'
            grants = self.session.execute(
                select(FormalDeletionGrantConsumption).where(
                    FormalDeletionGrantConsumption.grant_id.in_(grant_ids)
                ).with_for_update()
            ).scalars().all()
            for grant in grants:
                grant.state = 'closed'
            self._event(
                item,
                'purge.item.permits_cancelled_for_reprotection',
                now,
                result_code='reprotection_cancelled_before_delete',
            )
            self.session.commit()
            return True
        except Exception:
            self.session.rollback()
            return False

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
    def __init__(
        self,
        *,
        repository,
        capability,
        deleter,
        capability_context=None,
    ):
        self._repository = repository
        self._capability = capability
        self._deleter = deleter
        self._capability_context = capability_context

    def process_one_item(self) -> bool:
        """Hard gate precedes even a queue claim in T13."""
        if not self._capability_available():
            return False
        self._repository.reconcile_expired_authorizations()
        claim = self._repository.claim_next_item()
        if claim is None:
            return False
        try:
            checkpoint = self._value(claim, 'checkpoint', 'checkpoint')
            if checkpoint in ('pending', 'original_delete_started'):
                checkpoint = self._process_delete_operation(
                    claim,
                    operation_kind='original',
                    current_checkpoint=checkpoint,
                    pre_intent_checkpoint='pending',
                    completed_checkpoint='original_deleted',
                )
            if checkpoint in ('original_deleted', 'preview_delete_started'):
                if self._repository.preview_is_shared(claim):
                    self._repository.checkpoint(claim, 'preview_shared')
                else:
                    self._process_delete_operation(
                        claim,
                        operation_kind='preview',
                        current_checkpoint=checkpoint,
                        pre_intent_checkpoint='original_deleted',
                        completed_checkpoint='preview_deleted',
                    )
            self._require_capability()
            if not self._repository.finalize(claim):
                raise RuntimeError('formal purge finalization rejected')
            self._repository.checkpoint(claim, 'completed')
        except Exception as exc:
            failure = getattr(self._repository, 'fail', None)
            if failure is None:
                raise
            failure(
                claim,
                getattr(exc, 'error_code', 'PURGE_OBJECT_DELETE_FAILED'),
            )
        return True

    def _process_delete_operation(
        self,
        claim,
        *,
        operation_kind,
        current_checkpoint,
        pre_intent_checkpoint,
        completed_checkpoint,
    ):
        key = self._value(
            claim,
            'original' if operation_kind == 'original' else 'preview',
            'original_key' if operation_kind == 'original' else 'preview_key',
        )
        grant = self._require_capability()
        observation = self._deleter.observe(key)
        if current_checkpoint == pre_intent_checkpoint and observation is None:
            if operation_kind == 'original':
                raise OriginalMissingBeforeIntent()
            raise PreviewMissingBeforeIntent()
        if current_checkpoint != pre_intent_checkpoint:
            authorization = self._repository.resume_delete_intent(
                claim, operation_kind=operation_kind, grant=grant,
            )
            if authorization is None:
                raise RuntimeError(f'{operation_kind} absent replay unauthorized')
            if observation is None:
                if not self._repository.confirm_absent_after_intent(authorization):
                    raise RuntimeError(f'{operation_kind} absent replay unauthorized')
                return completed_checkpoint
        else:
            authorization = self._repository.begin_delete_intent(
                claim,
                operation_kind=operation_kind,
                observation=observation,
                grant=grant,
            )
        if authorization is None:
            raise RuntimeError(f'{operation_kind} delete intent unauthorized')
        self._require_capability()
        executing = self._repository.start_delete_call(authorization)
        if executing is None:
            raise RuntimeError(f'{operation_kind} delete permit rejected')
        self._require_capability()
        deleted = self._deleter.delete(executing)
        self._require_deletion_observation(deleted)
        if not self._repository.complete_delete_call(executing, deleted):
            raise RuntimeError(f'{operation_kind} delete completion rejected')
        return completed_checkpoint

    def _capability_available(self):
        try:
            return bool(self._capability.evaluate(self._capability_context))
        except TypeError:
            # T13 unit/integration fakes predate the typed context. Production
            # FileFormalDeletionCapabilitySource never uses this compatibility
            # branch and rejects a missing/mismatched context.
            try:
                return bool(self._capability.evaluate())
            except Exception:
                return False
        except Exception:
            return False

    def _require_capability(self):
        grant = self._evaluate_capability()
        if not grant:
            raise FormalDeletionCapabilityRevoked()
        return grant

    def _evaluate_capability(self):
        try:
            return self._capability.evaluate(self._capability_context)
        except TypeError:
            try:
                return self._capability.evaluate()
            except Exception:
                return None
        except Exception:
            return None

    @staticmethod
    def _require_deletion_observation(value):
        from services.purge_object_storage import DeletionObservation

        if not isinstance(value, DeletionObservation):
            raise RuntimeError('formal deleter returned invalid observation')

    @staticmethod
    def _value(claim, mapping_key, attribute):
        if isinstance(claim, dict):
            return claim.get(mapping_key, 'pending' if mapping_key == 'checkpoint' else None)
        return getattr(claim, attribute)


class FormalDeletionCapabilityRevoked(RuntimeError):
    error_code = 'PURGE_FORMAL_DELETION_DISABLED'


class OriginalMissingBeforeIntent(RuntimeError):
    error_code = 'PURGE_ORIGINAL_MISSING_BEFORE_INTENT'


class PreviewMissingBeforeIntent(RuntimeError):
    error_code = 'PURGE_PREVIEW_MISSING_BEFORE_INTENT'
