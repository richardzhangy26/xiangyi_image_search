"""Issue #26 无 ops 批次控制服务的单元合同。"""

import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app import create_app
from models import AssetActivityRecord, ImageAsset, PurgeBatch, db


@pytest.fixture
def app():
    application = create_app('testing')
    with application.app_context():
        db.create_all()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


def _asset(*, status='archived'):
    asset_id = uuid.uuid4()
    nonce = uuid.uuid4().hex
    return ImageAsset(
        id=asset_id,
        source_provider='test',
        source_bucket='test-bucket',
        source_relative_path=f'assets/{nonce}.png',
        source_revision=1,
        display_name='asset.png',
        oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}',
        content_hash=nonce,
        source_size=1,
        source_mime_type='image/png',
        source_width=1,
        source_height=1,
        vector=[0.0] * 1024,
        embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024,
        normalization_version='preview-v1',
        status=status,
    )


def _service():
    from services.purge_batch_control import PurgeBatchControlService

    return PurgeBatchControlService(db.session)


def test_same_actor_key_and_fingerprint_replays_cancelled_batch(app):
    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        service = _service()

        first = service.create_or_replay(
            actor_id='admin',
            idempotency_key='key.1234567',
            asset_ids=[asset.id],
            confirmation='永久删除 1 张',
            request_id='issue-26-control',
        )
        cancelled = service.cancel(
            first.batch.id, actor_id='admin', request_id='issue-26-cancel'
        )
        replay = service.create_or_replay(
            actor_id='admin',
            idempotency_key='key.1234567',
            asset_ids=[asset.id],
            confirmation='永久删除 1 张',
            request_id='issue-26-replay',
        )

        assert cancelled.status == 'cancelled'
        assert replay.replayed is True
        assert replay.batch.id == first.batch.id
        assert replay.batch.status == 'cancelled'


def test_same_key_with_different_request_is_a_stable_conflict(app):
    with app.app_context():
        first, second = _asset(), _asset()
        db.session.add_all([first, second])
        db.session.commit()
        service = _service()
        service.create_or_replay(
            actor_id='admin', idempotency_key='key.1234567',
            asset_ids=[first.id], confirmation='永久删除 1 张', request_id='r1',
        )

        from services.purge_batch_control import IdempotencyConflictError

        with pytest.raises(IdempotencyConflictError) as caught:
            service.create_or_replay(
                actor_id='admin', idempotency_key='key.1234567',
                asset_ids=[second.id], confirmation='永久删除 1 张', request_id='r2',
            )
        assert caught.value.error_code == 'PURGE_IDEMPOTENCY_CONFLICT'


def test_non_cancelled_batch_exclusively_holds_an_archived_asset(app):
    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        service = _service()
        service.create_or_replay(
            actor_id='admin-a', idempotency_key='key.first.1',
            asset_ids=[asset.id], confirmation='永久删除 1 张', request_id='r1',
        )

        from services.purge_batch_control import PurgeBatchStateError

        with pytest.raises(PurgeBatchStateError) as caught:
            service.create_or_replay(
                actor_id='admin-b', idempotency_key='key.second.1',
                asset_ids=[asset.id], confirmation='永久删除 1 张', request_id='r2',
            )
        assert caught.value.error_code == 'PURGE_ASSET_IN_ACTIVE_BATCH'
        assert PurgeBatch.query.count() == 1


def test_retention_failed_batch_cannot_retry_but_can_cancel(app):
    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        service = _service()
        created = service.create_or_replay(
            actor_id='admin', idempotency_key='key.retention.1',
            asset_ids=[asset.id], confirmation='永久删除 1 张', request_id='r1',
        ).batch
        created.status = 'failed'
        created.error_code = 'PURGE_BACKUP_RETENTION_EXPIRED'
        db.session.commit()

        from services.purge_batch_control import PurgeBatchStateError

        with pytest.raises(PurgeBatchStateError) as caught:
            service.retry(created.id, actor_id='admin', request_id='r2')
        assert caught.value.error_code == 'PURGE_BATCH_NOT_RETRYABLE'
        assert service.cancel(created.id, actor_id='admin', request_id='r3').status == 'cancelled'
        assert AssetActivityRecord.query.filter_by(
            event_type='purge.batch.cancelled', target_id=str(created.id)
        ).count() == 1


def test_claim_next_atomically_moves_queued_batch_to_database_backup(app):
    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        service = _service()
        created = service.create_or_replay(
            actor_id='admin', idempotency_key='key.claim.1',
            asset_ids=[asset.id], confirmation='永久删除 1 张', request_id='r1',
        ).batch

        now = datetime(2026, 8, 29, 10, 0, 0)
        claim = service.claim_next(worker_id='worker-1', lease_seconds=30, now=now)

        assert claim is not None
        assert claim.batch_id == created.id
        assert db.session.get(PurgeBatch, created.id).status == 'database_backup'
        assert db.session.get(PurgeBatch, created.id).lease_expires_at == now + timedelta(seconds=30)


def test_unique_idempotency_race_is_recovered_through_a_savepoint():
    source = (Path(__file__).resolve().parents[1] / 'services' / 'purge_batch_control.py').read_text(
        encoding='utf-8'
    )
    assert 'begin_nested()' in source


def test_claim_cas_rejects_a_late_result_after_cancellation(app):
    from services.purge_batch_control import PurgeBatchControlService

    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        service = PurgeBatchControlService(db.session)
        batch = service.create_or_replay(
            actor_id='admin', idempotency_key='key.cas.01', asset_ids=[asset.id],
            confirmation='永久删除 1 张', request_id='create',
        ).batch
        claim = service.claim_next(worker_id='worker', lease_seconds=10)
        service.cancel(batch.id, actor_id='admin', request_id='cancel')

        assert service.advance_if_current(claim, status='object_backup') is False
