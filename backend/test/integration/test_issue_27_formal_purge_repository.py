"""Formal purge item claiming/checkpoint contracts on real PostgreSQL."""

import uuid
from datetime import datetime, timedelta, timezone

from models import ImageAsset, PurgeBatch, PurgeBatchItem, db
from services.purge_object_storage import DeletionObservation, FormalObjectObservation
from test_support.formal_grant import StaticFormalCapability, formal_grant_for


def _asset():
    nonce = uuid.uuid4().hex
    return ImageAsset(
        source_provider='test', source_bucket='source',
        source_relative_path=f'purge/{nonce}.png', source_revision=1,
        display_name='purge.png', oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}', content_hash=nonce,
        source_size=1, source_mime_type='image/png', source_width=1,
        source_height=1, vector=[0.0] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024, normalization_version='preview-v1',
        status='archived',
    )


def _batch_item(asset):
    batch = PurgeBatch(
        actor_id='admin', idempotency_key=f'key.{uuid.uuid4().hex}',
        request_fingerprint_sha256='a' * 64, confirmation_text='永久删除 1 张',
        status='pending_deletion', retain_until=datetime.now() + timedelta(days=1),
        database_backup_id='purge-test', database_manifest_sha256='d' * 64,
        object_manifest_sha256='e' * 64,
    )
    item = PurgeBatchItem(
        batch=batch, target_asset_id=asset.id, ordinal=0,
        original_formal_key=asset.oss_path, original_backup_object_id='orig-copy',
        original_backup_sha256='b' * 64, preview_formal_key=asset.preview_oss_path,
        preview_backup_object_id='preview-copy', preview_backup_sha256='c' * 64,
        preview_delete_authorized=True,
        authorization_retain_until=datetime.now() + timedelta(days=1),
        formal_bucket='formal-test-bucket',
    )
    return batch, item


def _observation(item, operation_kind):
    return FormalObjectObservation(
        formal_bucket=item.formal_bucket,
        formal_key=(
            item.original_formal_key
            if operation_kind == 'original'
            else item.preview_formal_key
        ),
        size=1,
        sha256=(
            item.original_backup_sha256
            if operation_kind == 'original'
            else item.preview_backup_sha256
        ),
        etag=f'etag-{operation_kind}',
        observed_at=datetime.now(timezone.utc),
    )


class FakeFormalDeleter:
    def __init__(self, item, *, fail_preview_once=False):
        self.item = item
        self.fail_preview_once = fail_preview_once
        self.calls = []
        self.deleted = set()

    def observe(self, key):
        if key in self.deleted:
            return None
        operation = 'original' if key == self.item.original_formal_key else 'preview'
        return _observation(self.item, operation)

    def delete(self, authorization):
        key = authorization.formal_key
        self.calls.append(key)
        if (
            key == self.item.preview_formal_key
            and self.fail_preview_once
        ):
            self.fail_preview_once = False
            raise RuntimeError('preview down')
        self.deleted.add(key)
        return DeletionObservation(
            result='deleted',
            before=authorization.observation,
            deleted_at=datetime.now(timezone.utc),
            after_missing=True,
        )


def _complete_repository_delete(repo, authorization):
    executing = repo.start_delete_call(authorization)
    assert executing is not None
    deleted = DeletionObservation(
        result='deleted', before=authorization.observation,
        deleted_at=datetime.now(timezone.utc), after_missing=True,
    )
    assert repo.complete_delete_call(executing, deleted) is True


def test_claim_keeps_pending_batch_cancellable_until_original_intent(app):
    from services.formal_purge import FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()

    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item(worker_id='test', lease_seconds=60)
    assert claim is not None
    assert db.session.get(PurgeBatch, batch.id).status == 'pending_deletion'
    assert db.session.get(PurgeBatchItem, (batch.id, asset.id)).status == 'in_progress'

    assert repo.checkpoint(claim, 'original_delete_started') is True
    assert db.session.get(PurgeBatch, batch.id).status == 'deleting'


def test_fake_deleter_completes_authorized_item_and_removes_vector_row(app):
    from services.formal_purge import FormalPurgeRepository, FormalPurgeWorker

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    deleter = FakeFormalDeleter(item)
    grant = formal_grant_for(batch, item)

    worker = FormalPurgeWorker(
        repository=FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True),
        capability=StaticFormalCapability(grant), capability_context=grant.context,
        deleter=deleter,
    )
    assert worker.process_one_item() is True
    assert deleter.calls == [asset.oss_path, asset.preview_oss_path]
    assert db.session.get(ImageAsset, asset.id) is None
    completed = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert completed.status == 'completed'
    assert completed.original_delete_started_at is not None
    assert completed.original_deleted_at is not None
    assert completed.preview_delete_started_at is not None
    assert completed.preview_deleted_at is not None
    assert completed.database_deleted_at is not None
    assert db.session.get(PurgeBatch, batch.id).status == 'completed'


def test_partial_failure_keeps_checkpoint_and_retry_does_not_repeat_original(app):
    from services.formal_purge import FormalPurgeRepository, FormalPurgeWorker

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    deleter = FakeFormalDeleter(item, fail_preview_once=True)
    grant = formal_grant_for(batch, item)
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    worker = FormalPurgeWorker(
        repository=repo, capability=StaticFormalCapability(grant),
        capability_context=grant.context, deleter=deleter,
    )
    assert worker.process_one_item() is True
    failed = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert failed.status == 'failed'
    assert failed.checkpoint == 'preview_delete_started'
    assert db.session.get(ImageAsset, asset.id) is not None

    assert repo.retry_item(batch.id, asset.id) is True
    assert worker.process_one_item() is True
    assert deleter.calls.count(asset.oss_path) == 1
    assert db.session.get(ImageAsset, asset.id) is None


def test_shared_preview_is_retained_while_target_vector_row_is_removed(app):
    from services.formal_purge import FormalPurgeRepository, FormalPurgeWorker

    target = _asset()
    other = _asset()
    other.preview_oss_path = target.preview_oss_path
    db.session.add_all([target, other])
    db.session.flush()
    batch, item = _batch_item(target)
    db.session.add_all([batch, item])
    db.session.commit()
    deleter = FakeFormalDeleter(item)
    grant = formal_grant_for(batch, item)

    assert FormalPurgeWorker(
        repository=FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True),
        capability=StaticFormalCapability(grant), capability_context=grant.context,
        deleter=deleter,
    ).process_one_item() is True
    assert deleter.calls == [target.oss_path]
    assert db.session.get(ImageAsset, target.id) is None
    assert db.session.get(ImageAsset, other.id) is not None


def test_manifest_revalidation_failure_is_not_claimed_and_records_one_year_event(app):
    from models import PurgeItemEvent
    from services.formal_purge import FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()

    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: False)
    assert repo.claim_next_item() is None
    failed = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert failed.error_code == 'PURGE_BACKUP_REVALIDATION_FAILED'
    event = PurgeItemEvent.query.filter_by(
        batch_id=batch.id, target_asset_id=asset.id,
        event_type='purge.item.failed',
    ).one()
    assert event.audit_retain_until >= event.created_at + timedelta(days=365)


def test_nonretryable_failure_reduces_batch_to_partial_failure(app):
    from services.formal_purge import FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    assert repo.checkpoint(claim, 'original_delete_started') is True
    assert repo.fail(claim, 'PURGE_REPROTECTION_REQUIRED', retryable=False) is True
    assert db.session.get(PurgeBatch, batch.id).status == 'partial_failure'


def _held_fences(item):
    from models import PurgeObjectFence

    now = datetime.now()
    return (
        PurgeObjectFence(
            formal_bucket=item.formal_bucket,
            formal_key=item.original_formal_key,
            kind='source_image',
            batch_id=item.batch_id,
            target_asset_id=item.target_asset_id,
            state='held',
            acquired_at=now,
            audit_retain_until=now + timedelta(days=365),
        ),
        PurgeObjectFence(
            formal_bucket=item.formal_bucket,
            formal_key=item.preview_formal_key,
            kind='search_preview',
            batch_id=item.batch_id,
            target_asset_id=item.target_asset_id,
            state='held',
            acquired_at=now,
            audit_retain_until=now + timedelta(days=365),
        ),
    )


def test_nonretryable_fail_before_intent_releases_held_fences(app):
    from models import PurgeObjectFence
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    db.session.add_all(_held_fences(item))
    db.session.commit()

    assert repo.fail(claim, 'PURGE_OBJECT_DELETE_FAILED', retryable=False) is True

    failed = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert failed.status == 'failed'
    assert failed.result_code == 'nonretryable'
    assert failed.error_code == 'PURGE_OBJECT_DELETE_FAILED'
    assert PurgeObjectFence.query.filter_by(
        batch_id=batch.id, target_asset_id=asset.id, state='held',
    ).count() == 0
    assert PurgeObjectFence.query.filter_by(
        batch_id=batch.id, target_asset_id=asset.id, state='released',
    ).count() == 2


def test_nonretryable_fail_after_delete_started_keeps_fences_and_requires_reprotection(app):
    from models import PurgeObjectFence
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    assert repo.begin_delete_intent(
        claim, operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    ) is not None

    assert repo.fail(claim, 'PURGE_OBJECT_DELETE_FAILED', retryable=False) is True

    failed = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert failed.status == 'failed'
    assert failed.result_code == 'nonretryable'
    assert failed.error_code == 'PURGE_REPROTECTION_REQUIRED'
    assert PurgeObjectFence.query.filter_by(
        batch_id=batch.id, target_asset_id=asset.id, state='held',
    ).count() == 2


def test_authorize_delete_call_acquires_complete_fence_after_first_intent(app):
    from services.formal_purge import FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    authorized = repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    )
    assert authorized is not None
    assert len(authorized.fence_ids) == 2


def test_authorize_returns_typed_call_bound_to_actual_fences_and_object_bytes(app):
    from models import PurgeObjectFence
    from services.formal_purge import DeleteCallAuthorization, FormalPurgeRepository
    from services.purge_object_storage import FormalObjectObservation

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    observation = FormalObjectObservation(
        formal_bucket=item.formal_bucket,
        formal_key=item.original_formal_key,
        size=asset.source_size,
        sha256=item.original_backup_sha256,
        etag='etag-original',
        observed_at=datetime.now().astimezone(),
    )

    authorized = repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=observation,
        grant=formal_grant_for(batch, item),
    )

    assert isinstance(authorized, DeleteCallAuthorization)
    actual_ids = tuple(sorted(
        fence.id
        for fence in PurgeObjectFence.query.filter_by(
            batch_id=batch.id, target_asset_id=asset.id, state='held',
        ).all()
    ))
    assert authorized.fence_ids == actual_ids
    assert authorized.batch_id == batch.id
    assert authorized.target_asset_id == asset.id
    assert authorized.operation_kind == 'original'
    assert authorized.observation == observation


def test_begin_delete_intent_atomically_consumes_grant_fences_and_permit(app):
    from models import (
        FormalDeleteCallPermit,
        FormalDeletionGrantConsumption,
        PurgeObjectFence,
    )
    from services.formal_purge import DeleteCallAuthorization, FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    grant = formal_grant_for(batch, item)

    authorization = repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=grant,
    )

    assert isinstance(authorization, DeleteCallAuthorization)
    assert authorization.permit_id is not None
    persisted_item = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert persisted_item.checkpoint == 'original_delete_started'
    assert db.session.get(PurgeBatch, batch.id).status == 'deleting'
    consumption = db.session.get(FormalDeletionGrantConsumption, grant.grant_id)
    assert consumption.batch_id == batch.id
    assert consumption.used_object_deletes == 1
    permit = db.session.get(FormalDeleteCallPermit, authorization.permit_id)
    assert permit.state == 'issued'
    assert permit.grant_id == grant.grant_id
    assert PurgeObjectFence.query.filter_by(
        batch_id=batch.id, target_asset_id=asset.id, state='held',
    ).count() == 2


def test_delete_permit_enters_executing_once_without_reconsuming_grant(app):
    from models import FormalDeleteCallPermit, FormalDeletionGrantConsumption
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    grant = formal_grant_for(batch, item)
    authorization = repo.begin_delete_intent(
        claim, operation_kind='original',
        observation=_observation(item, 'original'), grant=grant,
    )

    executing = repo.start_delete_call(authorization)
    repeated = repo.start_delete_call(executing)

    assert executing is not None and repeated is not None
    permit = db.session.get(FormalDeleteCallPermit, authorization.permit_id)
    consumption = db.session.get(FormalDeletionGrantConsumption, grant.grant_id)
    assert permit.state == 'executing'
    assert permit.executing_at is not None
    assert consumption.used_object_deletes == 1


def test_start_delete_call_rejects_handmade_authorization_without_delete(app):
    from dataclasses import replace
    from models import FormalDeleteCallPermit
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    deleter = FakeFormalDeleter(item)
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    authorization = repo.begin_delete_intent(
        claim, operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    )
    assert authorization is not None

    random_permit = replace(authorization, permit_id=uuid.uuid4())
    swapped_fences = replace(
        authorization,
        fence_ids=(authorization.fence_ids[1], authorization.fence_ids[0]),
    )
    expired = replace(
        authorization,
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    assert repo.start_delete_call(random_permit) is None
    assert repo.start_delete_call(swapped_fences) is None
    assert repo.start_delete_call(expired) is None
    assert deleter.calls == []
    permit = db.session.get(FormalDeleteCallPermit, authorization.permit_id)
    assert permit.state == 'issued'


def test_start_delete_call_revalidates_manifest_after_locking_permit(app):
    from models import FormalDeleteCallPermit
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    allowed = {'ok': True}
    deleter = FakeFormalDeleter(item)
    repo = FormalPurgeRepository(
        db.session, manifest_validator=lambda _b, _i: allowed['ok'],
    )
    claim = repo.claim_next_item()
    authorization = repo.begin_delete_intent(
        claim, operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    )
    assert authorization is not None
    allowed['ok'] = False

    assert repo.start_delete_call(authorization) is None
    assert deleter.calls == []
    permit = db.session.get(FormalDeleteCallPermit, authorization.permit_id)
    assert permit.state == 'issued'


def test_crash_reclaim_404_requires_existing_permit_and_fences_before_checkpoint(app):
    from models import FormalDeleteCallPermit, FormalDeletionGrantConsumption
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    first_claim = repo.claim_next_item()
    grant = formal_grant_for(batch, item)
    first = repo.begin_delete_intent(
        first_claim, operation_kind='original',
        observation=_observation(item, 'original'), grant=grant,
    )
    db.session.query(PurgeBatchItem).filter_by(
        batch_id=batch.id, target_asset_id=asset.id,
    ).update({'lease_expires_at': datetime.now() - timedelta(seconds=1)})
    db.session.commit()

    reclaimed = repo.claim_next_item()
    assert reclaimed.claim_generation == first_claim.claim_generation + 1
    resumed = repo.resume_delete_intent(
        reclaimed, operation_kind='original', grant=grant,
    )
    assert resumed is not None
    assert resumed.permit_id == first.permit_id
    assert repo.confirm_absent_after_intent(resumed) is True

    row = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    permit = db.session.get(FormalDeleteCallPermit, first.permit_id)
    consumption = db.session.get(FormalDeletionGrantConsumption, grant.grant_id)
    assert row.checkpoint == 'original_deleted'
    assert permit.state == 'completed'
    assert permit.result_code == 'already_absent_after_intent'
    assert consumption.used_object_deletes == 1


def test_begin_retry_reuses_permit_and_same_grant_id_cannot_cross_batch(app):
    from models import FormalDeleteCallPermit, FormalDeletionGrantConsumption
    from services.formal_purge import FormalPurgeRepository

    first_asset = _asset(); db.session.add(first_asset); db.session.flush()
    first_batch, first_item = _batch_item(first_asset)
    db.session.add_all([first_batch, first_item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    first_claim = repo.claim_next_item()
    grant = formal_grant_for(first_batch, first_item)
    first = repo.begin_delete_intent(
        first_claim, operation_kind='original',
        observation=_observation(first_item, 'original'), grant=grant,
    )
    repeated = repo.begin_delete_intent(
        first_claim, operation_kind='original',
        observation=_observation(first_item, 'original'), grant=grant,
    )
    assert repeated.permit_id == first.permit_id
    assert db.session.get(
        FormalDeletionGrantConsumption, grant.grant_id,
    ).used_object_deletes == 1
    assert FormalDeleteCallPermit.query.filter_by(grant_id=grant.grant_id).count() == 1

    second_asset = _asset(); db.session.add(second_asset); db.session.flush()
    second_batch, second_item = _batch_item(second_asset)
    db.session.add_all([second_batch, second_item]); db.session.commit()
    second_claim = repo.claim_next_item()
    forged = formal_grant_for(
        second_batch, second_item, grant_id=grant.grant_id,
    )
    assert repo.begin_delete_intent(
        second_claim, operation_kind='original',
        observation=_observation(second_item, 'original'), grant=forged,
    ) is None
    assert db.session.get(PurgeBatchItem, (
        second_batch.id, second_asset.id,
    )).checkpoint == 'pending'
    assert FormalDeleteCallPermit.query.filter_by(
        batch_id=second_batch.id,
    ).count() == 0


def test_expired_in_progress_item_is_reclaimed_with_new_generation_and_token(app):
    from services.formal_purge import FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    item.status = 'in_progress'
    item.claim_token = uuid.uuid4()
    item.claim_generation = 4
    item.lease_expires_at = datetime.now() - timedelta(seconds=1)
    db.session.add_all([batch, item])
    db.session.commit()
    old = item.claim_token
    claim = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True).claim_next_item()
    assert claim.claim_generation == 5
    assert claim.claim_token != old


def test_expired_claim_cannot_checkpoint_or_authorize_delete(app):
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    db.session.execute(__import__('sqlalchemy').text("UPDATE purge_batch_items SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE batch_id = :b"), {'b': str(batch.id)})
    db.session.commit()
    assert repo.checkpoint(claim, 'original_delete_started') is False
    assert repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    ) is None


def test_complete_set_fences_remain_held_until_tombstone_finalizes(app):
    from models import PurgeObjectFence
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    grant = formal_grant_for(batch, item)
    authorization = repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=grant,
    )
    assert authorization is not None
    _complete_repository_delete(repo, authorization)
    repo.checkpoint(claim, 'preview_shared')
    assert PurgeObjectFence.query.filter_by(batch_id=batch.id, state='held').count() == 2
    assert repo.finalize(claim) is True
    assert PurgeObjectFence.query.filter_by(batch_id=batch.id, state='held').count() == 0


def test_authorize_original_delete_rejects_current_other_asset_reference(app):
    from models import ImageImportItem
    from services.formal_purge import FormalPurgeRepository

    target = _asset(); db.session.add(target); db.session.flush()
    ref = ImageImportItem(
        source_provider='test', source_bucket='source', source_relative_path=f'import/{uuid.uuid4().hex}.png',
        source_revision=1, display_name='ref.png', oss_path=target.oss_path,
        preview_oss_path='preview/other', content_hash=uuid.uuid4().hex, source_size=1,
        source_mime_type='image/png', source_width=1, source_height=1,
        normalization_version='preview-v1', request_id='ref', status='queued',
    )
    db.session.add(ref)
    batch, item = _batch_item(target); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    assert repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    ) is None


def test_authorize_preview_delete_rejects_current_shared_asset_reference(app):
    from services.formal_purge import FormalPurgeRepository

    target = _asset(); other = _asset(); other.preview_oss_path = target.preview_oss_path
    db.session.add_all([target, other]); db.session.flush()
    batch, item = _batch_item(target); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    grant = formal_grant_for(batch, item)
    original_authorization = repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=grant,
    )
    assert original_authorization is not None
    _complete_repository_delete(repo, original_authorization)
    assert repo.begin_delete_intent(
        claim,
        operation_kind='preview',
        observation=_observation(item, 'preview'),
        grant=grant,
    ) is None


def test_expired_post_intent_item_requires_reprotection_and_keeps_fences(app):
    from models import PurgeObjectFence
    from services.formal_purge import FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    assert repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    )
    db.session.query(PurgeBatch).filter_by(id=batch.id).update({
        'retain_until': datetime.now() - timedelta(seconds=1),
    })
    db.session.query(PurgeBatchItem).filter_by(
        batch_id=batch.id, target_asset_id=asset.id,
    ).update({
        'authorization_retain_until': datetime.now() - timedelta(seconds=1),
    })
    db.session.commit()

    assert repo.reconcile_expired_authorizations(limit=10) == 1

    expired_batch = db.session.get(PurgeBatch, batch.id)
    expired = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert expired_batch.status == 'partial_failure'
    assert expired.status == 'failed'
    assert expired.result_code == 'nonretryable'
    assert expired.error_code == 'PURGE_REPROTECTION_REQUIRED'
    assert expired.claim_token is None
    assert PurgeObjectFence.query.filter_by(
        batch_id=batch.id, target_asset_id=asset.id, state='held',
    ).count() == 2


def test_exact_reprotection_releases_fences_and_marks_item_reprotected(app):
    from models import PurgeObjectFence
    from services.formal_purge import FormalPurgeRepository

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    repo.begin_delete_intent(
        claim,
        operation_kind='original',
        observation=_observation(item, 'original'),
        grant=formal_grant_for(batch, item),
    )
    db.session.query(PurgeBatch).filter_by(id=batch.id).update({
        'retain_until': datetime.now() - timedelta(seconds=1),
    })
    db.session.query(PurgeBatchItem).filter_by(
        batch_id=batch.id, target_asset_id=asset.id,
    ).update({
        'authorization_retain_until': datetime.now() - timedelta(seconds=1),
    })
    db.session.commit()
    repo.reconcile_expired_authorizations(limit=10)

    wrong = _observation(item, 'original')
    wrong = FormalObjectObservation(
        **{**wrong.__dict__, 'sha256': '9' * 64}
    )
    assert repo.confirm_reprotected(
        batch.id,
        asset.id,
        original_observation=wrong,
        preview_observation=_observation(item, 'preview'),
    ) is False
    assert PurgeObjectFence.query.filter_by(batch_id=batch.id, state='held').count() == 2

    assert repo.cancel_issued_permits_for_reprotection(
        batch.id, asset.id,
    ) is True

    assert repo.confirm_reprotected(
        batch.id,
        asset.id,
        original_observation=_observation(item, 'original'),
        preview_observation=_observation(item, 'preview'),
    ) is True
    protected = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert protected.status == 'failed'
    assert protected.result_code == 'reprotected'
    assert protected.error_code == 'PURGE_BACKUP_RETENTION_EXPIRED'
    assert PurgeObjectFence.query.filter_by(batch_id=batch.id, state='held').count() == 0


def test_reprotection_requires_explicit_issued_permit_cancel_and_rejects_executing(app):
    from models import FormalDeleteCallPermit, PurgeObjectFence
    from services.formal_purge import FormalPurgeRepository

    def expired_with_permit(*, executing):
        asset = _asset(); db.session.add(asset); db.session.flush()
        batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
        repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
        claim = repo.claim_next_item()
        authorization = repo.begin_delete_intent(
            claim, operation_kind='original',
            observation=_observation(item, 'original'),
            grant=formal_grant_for(batch, item),
        )
        if executing:
            assert repo.start_delete_call(authorization) is not None
        db.session.query(PurgeBatch).filter_by(id=batch.id).update({
            'retain_until': datetime.now() - timedelta(seconds=1),
        })
        db.session.query(PurgeBatchItem).filter_by(
            batch_id=batch.id, target_asset_id=asset.id,
        ).update({
            'authorization_retain_until': datetime.now() - timedelta(seconds=1),
        })
        db.session.commit()
        repo.reconcile_expired_authorizations(limit=10)
        return repo, asset, batch, item, authorization

    issued_repo, issued_asset, issued_batch, issued_item, issued_auth = expired_with_permit(
        executing=False,
    )
    assert issued_repo.confirm_reprotected(
        issued_batch.id, issued_asset.id,
        original_observation=_observation(issued_item, 'original'),
        preview_observation=_observation(issued_item, 'preview'),
    ) is False
    assert issued_repo.cancel_issued_permits_for_reprotection(
        issued_batch.id, issued_asset.id,
    ) is True
    assert db.session.get(FormalDeleteCallPermit, issued_auth.permit_id).state == 'cancelled'
    assert issued_repo.confirm_reprotected(
        issued_batch.id, issued_asset.id,
        original_observation=_observation(issued_item, 'original'),
        preview_observation=_observation(issued_item, 'preview'),
    ) is True

    executing_repo, executing_asset, executing_batch, executing_item, _ = expired_with_permit(
        executing=True,
    )
    assert executing_repo.cancel_issued_permits_for_reprotection(
        executing_batch.id, executing_asset.id,
    ) is False
    assert executing_repo.confirm_reprotected(
        executing_batch.id, executing_asset.id,
        original_observation=_observation(executing_item, 'original'),
        preview_observation=_observation(executing_item, 'preview'),
    ) is False
    assert PurgeObjectFence.query.filter_by(
        batch_id=executing_batch.id, state='held',
    ).count() == 2
