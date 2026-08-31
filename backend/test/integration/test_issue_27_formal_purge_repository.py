"""Formal purge item claiming/checkpoint contracts on real PostgreSQL."""

import uuid
from datetime import datetime, timedelta

from models import ImageAsset, PurgeBatch, PurgeBatchItem, db


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
    calls = []

    class Capability:
        def evaluate(self):
            return True

    class Deleter:
        def delete_if_present(self, key):
            calls.append(key)
            return 'deleted'

    worker = FormalPurgeWorker(
        repository=FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True),
        capability=Capability(), deleter=Deleter(),
    )
    assert worker.process_one_item() is True
    assert calls == [asset.oss_path, asset.preview_oss_path]
    assert db.session.get(ImageAsset, asset.id) is None
    completed = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert completed.status == 'completed'
    assert db.session.get(PurgeBatch, batch.id).status == 'completed'


def test_partial_failure_keeps_checkpoint_and_retry_does_not_repeat_original(app):
    from services.formal_purge import FormalPurgeRepository, FormalPurgeWorker

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch, item = _batch_item(asset)
    db.session.add_all([batch, item])
    db.session.commit()
    calls = []

    class Capability:
        def evaluate(self):
            return True

    class FlakyDeleter:
        def __init__(self):
            self.preview_fail = True

        def delete_if_present(self, key):
            calls.append(key)
            if key == asset.preview_oss_path and self.preview_fail:
                self.preview_fail = False
                raise RuntimeError('preview down')
            return 'deleted'

    deleter = FlakyDeleter()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    worker = FormalPurgeWorker(repository=repo, capability=Capability(), deleter=deleter)
    assert worker.process_one_item() is True
    failed = db.session.get(PurgeBatchItem, (batch.id, asset.id))
    assert failed.status == 'failed'
    assert failed.checkpoint == 'preview_delete_started'
    assert db.session.get(ImageAsset, asset.id) is not None

    assert repo.retry_item(batch.id, asset.id) is True
    assert worker.process_one_item() is True
    assert calls.count(asset.oss_path) == 1
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
    calls = []

    class Capability:
        def evaluate(self):
            return True

    class Deleter:
        def delete_if_present(self, key):
            calls.append(key)
            return 'deleted'

    assert FormalPurgeWorker(
        repository=FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True),
        capability=Capability(), deleter=Deleter(),
    ).process_one_item() is True
    assert calls == [target.oss_path]
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
    assert repo.checkpoint(claim, 'original_delete_started') is True
    authorized = repo.authorize_delete_call(
        claim, 'original_delete_started', {'verified': True}, 'original',
    )
    assert authorized is not None
    assert len(authorized) == 2


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
    assert repo.authorize_delete_call(claim, 'original_delete_started', {'verified': True}, 'original') is None


def test_complete_set_fences_remain_held_until_tombstone_finalizes(app):
    from models import PurgeObjectFence
    from services.formal_purge import FormalPurgeRepository

    asset = _asset(); db.session.add(asset); db.session.flush()
    batch, item = _batch_item(asset); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item()
    repo.checkpoint(claim, 'original_delete_started')
    assert repo.authorize_delete_call(claim, 'original_delete_started', {'verified': True}, 'original')
    repo.checkpoint(claim, 'original_deleted')
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
    claim = repo.claim_next_item(); repo.checkpoint(claim, 'original_delete_started')
    assert repo.authorize_delete_call(claim, 'original_delete_started', {'verified': True}, 'original') is None


def test_authorize_preview_delete_rejects_current_shared_asset_reference(app):
    from services.formal_purge import FormalPurgeRepository

    target = _asset(); other = _asset(); other.preview_oss_path = target.preview_oss_path
    db.session.add_all([target, other]); db.session.flush()
    batch, item = _batch_item(target); db.session.add_all([batch, item]); db.session.commit()
    repo = FormalPurgeRepository(db.session, manifest_validator=lambda _b, _i: True)
    claim = repo.claim_next_item(); repo.checkpoint(claim, 'original_delete_started')
    assert repo.authorize_delete_call(claim, 'original_delete_started', {'verified': True}, 'original')
    repo.checkpoint(claim, 'original_deleted'); repo.checkpoint(claim, 'preview_delete_started')
    assert repo.authorize_delete_call(claim, 'preview_delete_started', {'verified': True}, 'preview') is None
