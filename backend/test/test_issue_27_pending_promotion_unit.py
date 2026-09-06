import uuid
from datetime import datetime, timedelta, timezone

import pytest

from models import ImageAsset, PurgeBatch, PurgeBatchItem, db
from services.purge_batch_control import PurgeBatchControlService


def _asset():
    nonce = uuid.uuid4().hex
    return ImageAsset(
        source_provider='test', source_bucket='source', source_relative_path=f'{nonce}.png',
        source_revision=1, display_name='x.png', oss_path=f'original/{nonce}',
        preview_oss_path=f'preview/{nonce}', content_hash=nonce, source_size=1,
        source_mime_type='image/png', source_width=1, source_height=1,
        vector=[0.0] * 1024, embedding_model='tongyi-embedding-vision-plus-2026-03-06',
        embedding_dimension=1024, normalization_version='preview-v1', status='archived',
    )


def test_verified_promotion_requires_complete_authorization_for_every_item(tmp_path):
    from app import create_app
    app = create_app('testing')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite://'
    with app.app_context():
        db.create_all()
        _assert_incomplete_promotion()


def _assert_incomplete_promotion():
    from services.purge_formal_authorization import (
        FormalPurgeAuthorizationBundle,
    )

    asset = _asset()
    db.session.add(asset)
    db.session.flush()
    batch = PurgeBatch(
        actor_id='a', idempotency_key='key.promote.1', request_fingerprint_sha256='a' * 64,
        confirmation_text='永久删除 1 张', status='verifying',
        retain_until=datetime.now() + timedelta(days=1),
        database_backup_id='purge-test', database_manifest_sha256='c' * 64,
        object_manifest_sha256='b' * 64,
    )
    item = PurgeBatchItem(batch=batch, target_asset_id=asset.id, ordinal=0, status='queued')
    db.session.add_all([batch, item])
    db.session.commit()

    service = PurgeBatchControlService(db.session)
    with pytest.raises(ValueError):
        service.advance_verified_to_pending_if_current(
            FormalPurgeAuthorizationBundle(
                purge_batch_id=batch.id,
                manifest_sha256='b' * 64,
                database_backup_id='purge-test',
                database_manifest_sha256='c' * 64,
                retain_until=batch.retain_until.replace(tzinfo=timezone.utc),
                items=(),
            )
        )
    assert db.session.get(PurgeBatch, batch.id).status == 'verifying'
