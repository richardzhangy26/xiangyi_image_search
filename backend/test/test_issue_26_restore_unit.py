import uuid

import pytest

from app import create_app
from models import AssetActivityRecord, ImageAsset, PurgeBatch, PurgeBatchItem, db
from services.asset_recycle_bin import restore_image_assets
from services.purge_batch_control import PurgeBatchControlService


@pytest.fixture
def app():
    application = create_app('testing')
    with application.app_context():
        db.create_all()
    yield application
    with application.app_context():
        db.session.remove()
        db.drop_all()


def _asset():
    nonce = uuid.uuid4().hex
    return ImageAsset(
        id=uuid.uuid4(),
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
        status='archived',
    )


def test_restore_rejects_archived_asset_held_by_failed_batch_and_records_reference(app):
    from services.asset_recycle_bin import RestoreBlockedByPurgeBatch

    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        created = PurgeBatchControlService(db.session).create_or_replay(
            actor_id='admin',
            idempotency_key='key.restore.1',
            asset_ids=[asset.id],
            confirmation='永久删除 1 张',
            request_id='create',
        ).batch
        created.status = 'failed'
        created.error_code = 'PURGE_DATABASE_BACKUP_FAILED'
        db.session.commit()
        batch_id = created.id

        with pytest.raises(RestoreBlockedByPurgeBatch) as caught:
            restore_image_assets(
                db.session, [str(asset.id)], actor_id='admin', request_id='r1',
            )
        assert caught.value.error_code == 'PURGE_ASSET_RESTORE_BLOCKED'
        assert db.session.get(ImageAsset, asset.id).status == 'archived'
        record = AssetActivityRecord.query.filter_by(
            error_code='PURGE_ASSET_RESTORE_BLOCKED',
        ).one()
        assert record.after_state['batch_id'] == str(batch_id)


def test_cancelled_batch_allows_existing_restore_behavior(app):
    with app.app_context():
        asset = _asset()
        db.session.add(asset)
        db.session.commit()
        service = PurgeBatchControlService(db.session)
        created = service.create_or_replay(
            actor_id='admin',
            idempotency_key='key.restore.2',
            asset_ids=[asset.id],
            confirmation='永久删除 1 张',
            request_id='create',
        ).batch
        service.cancel(created.id, actor_id='admin', request_id='cancel')

        restored = restore_image_assets(
            db.session, [str(asset.id)], actor_id='admin', request_id='r2',
        )
        assert restored.status == 'succeeded'
        assert restored.restored_count == 1
        assert db.session.get(ImageAsset, asset.id).status == 'active'


def test_reprotected_partial_failure_no_longer_blocks_restore(app):
    with app.app_context():
        asset = _asset()
        batch = PurgeBatch(
            actor_id='admin', idempotency_key='key.restore.reprotected',
            request_fingerprint_sha256='a' * 64,
            confirmation_text='永久删除 1 张', status='partial_failure',
        )
        item = PurgeBatchItem(
            batch=batch, target_asset_id=asset.id, ordinal=0,
            status='failed', result_code='reprotected',
            error_code='PURGE_BACKUP_RETENTION_EXPIRED',
        )
        db.session.add_all([asset, batch, item])
        db.session.commit()

        restored = restore_image_assets(
            db.session, [str(asset.id)], actor_id='admin', request_id='r3',
        )

        assert restored.status == 'succeeded'
        assert db.session.get(ImageAsset, asset.id).status == 'active'
